#!/usr/bin/env python
"""Export the LIVE lane's recorded events per benched symbol-day (Ross Parity Bench).

WHY THIS EXISTS
---------------
The bench grades two sides with one ladder: the RECORDED side (what CHILI's live lane
actually did on that symbol-day) and the REPLAY side (what the replay driver did in the
harness).  The replay side arrives in the driver receipt's ``events`` block.  The recorded
side had no producer at all, so ``rossbench_report.load_recorded_events`` always found
nothing, fell back to the ledger's hand-written ``xref_verdict``, and — for the verdicts
that do not identify a rung — printed a bare ``unknown``.

MEASURED 2026-09-04 over ``scripts/build_ross_manifest.build()`` (418 windows):
``never_armed`` 65 and ``not_in_universe`` 29 place on the ladder from the verdict alone,
but ``armed_no_entry`` 13 and ``entered_wrong_leg`` 1 do not — and one of those two,
``armed_no_entry``, is the second-most-common non-null token in the corpus.
``ross_bench_scoring.XREF_VERDICT_PLACEMENT`` now names each refusal instead of hiding it,
and THIS script removes the refusal: with events in the window the scorer walks the ladder
and never consults the verdict at all.

WHAT IT WRITES
--------------
``<run-dir>/<SYMBOL>_<DATE>/recorded_events.jsonl`` — one JSON object per line::

    {"ts": "...", "event_type": "...", "payload": {...}, "session_id": N, "mode": "live"}

``ts``/``event_type``/``payload`` are the three keys ``classify_first_divergence`` reads
(``ross_bench_scoring._EVENT_TS_KEYS`` / ``_EVENT_TYPE_KEYS`` / ``_EVENT_PAYLOAD_KEYS``);
``session_id`` and ``mode`` are extra provenance the scorer ignores and an auditor needs.
It is the same object shape the driver writes into its own receipt
(scripts/replay_v3_fsm_window.py:1132-1136), so both sides of the bench are one shape.

Also ``recorded_events.meta.json`` beside it: the exact bounds queried, the sessions found,
the per-mode counts, and the payload filter that was applied.  The reporter reads only the
``.jsonl`` (``rossbench_report._RECORDED_EVENT_FILES``); the meta exists so a zero-row
export can be told apart from a missing one.

PAYLOAD PARITY
--------------
By default a recorded payload is filtered by exactly the same allow-list the driver applies
to its own receipt: ``_load_bearing_payload`` (imported from
scripts/export_replay_v3_parity_fixtures.py) UNION ``_BENCH_PAYLOAD_KEYS`` (read out of
scripts/replay_v3_fsm_window.py's SOURCE with ``ast`` — that module cannot be imported,
because it raises ``SystemExit`` at import time unless ``TEST_DATABASE_URL`` names a
``_test`` database, replay_v3_fsm_window.py:147-150).  Grading a rich recorded payload
against a filtered replay payload would make the two sides answer different questions.
``--full-payload`` opts out and says so in the meta.

SAFETY
------
Read-only: the connection is opened ``readonly=True``, the session is pinned to UTC (naive
text against a timestamp column otherwise resolves through the SESSION zone — the same trap
scripts/rossbench_pin_ross_events.open_hydrated_conn documents), and every statement carries a
``statement_timeout`` so a slow scan cannot sit in the live DB.  No table is written.

Usage:
  python scripts/rossbench_export_recorded_events.py --run-dir <bench out-dir> \\
      --dsn postgresql://chili:chili@localhost:5433/chili
  python scripts/rossbench_export_recorded_events.py --out-dir runs/x \\
      --cases SDOT:2026-06-26,IPST:2026-08-17 --dsn ...
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

EXPORT_SCHEMA = "chili.ross_recorded_events_export.v1"

# The filename the reporter looks for. Kept in sync deliberately: it is the FIRST ``.jsonl``
# name in rossbench_report._RECORDED_EVENT_FILES, and this script is the only producer.
EVENTS_FILENAME = "recorded_events.jsonl"
META_FILENAME = "recorded_events.meta.json"

# The driver source the payload allow-list is read out of.
DRIVER_SOURCE = os.path.join(_REPO_ROOT, "scripts", "replay_v3_fsm_window.py")

# The ledger's dates are ET trading days (build_ross_manifest writes ``window_et``), so a
# case's day boundary is an ET midnight, not a UTC one.
ET_ZONE_NAME = "America/New_York"

# Default mode filter. The ledger's xref layer audits the LIVE lane, and interleaving a
# paper session's events with a live session's into ONE chronological stream would
# synthesise a lifecycle no single lane had — the ladder would report an arm from one lane
# clearing a rung for the other. ``--mode all`` opts out; the meta reports the modes SEEN
# either way, so a symbol-day with only paper sessions shows up as such instead of as an
# empty export.
DEFAULT_MODES = ("live",)
MODE_ALL = "all"

# A liveness fence, not a tuned performance number: the db_watchdog kills backends more
# than 10 minutes past query_start (GOTCHA 11, scripts/replay_v3_fsm_window.py:539), so any
# statement this tool issues must die well before that on its own.
DEFAULT_STATEMENT_TIMEOUT_MS = 60_000

# The runner names a DISAMBIGUATED case dir ``SYMBOL@<selector-slug>_<YYYY-MM-DD>``
# (Case.dirname / Case.selector_slug, scripts/ross_replay_bench.py), because 62 of 217
# symbol-days carry more than one manifest row. Without the ``@`` infix here this tool
# skipped every one of them -- 5 of the 8 lane-alive known answers exported nothing and
# the reporter silently fell back to xref_verdict, which is the exact defect this script
# was written to close.
_CASE_DIR_RE = re.compile(
    r"^(?P<symbol>[A-Za-z0-9.\-]+?)(?:@(?P<selector>[A-Za-z0-9._\-]+))?_(?P<date>\d{4}-\d{2}-\d{2})$"
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ─────────────────────────────────────────────────────────────────────────────
# CASE SELECTION
# ─────────────────────────────────────────────────────────────────────────────

def parse_cases(spec: str) -> list[tuple[str, str, str]]:
    """``"SDOT:2026-06-26,IPST:2026-08-17"`` -> ``[("SDOT", "2026-06-26"), ...]``.

    Same ``SYMBOL:DATE`` spelling the bench runner's ``--cases`` uses
    (scripts/ross_replay_bench.py ``parse_case_spec``), so an operator can paste one list
    into both tools. Malformed entries are refused by name rather than skipped: a silently
    dropped case would produce a report whose recorded column is empty for a reason nobody
    can see.
    """
    out: list[tuple[str, str, str]] = []
    for chunk in (spec or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        symbol, sep, date = chunk.partition(":")
        symbol = symbol.strip().upper()
        date = date.strip()
        if not sep or not symbol or not _DATE_RE.match(date):
            raise SystemExit(
                f"[rossbench_export_recorded_events] --cases {chunk!r}: expected "
                "SYMBOL:YYYY-MM-DD"
            )
        # An UNAMBIGUOUS name here: the manifest route has no selector, so the directory is
        # the plain <SYMBOL>_<DATE> the runner uses when a symbol-day has one row.
        if (symbol, date) not in {(a, b) for a, b, _ in out}:
            out.append((symbol, date, f"{symbol}_{date}"))
    return out


def cases_from_run_dir(run_dir: str) -> list[tuple[str, str, str]]:
    """Case dirs under a bench out-dir, as ``(symbol, date)``.

    The runner names them ``<SYMBOL>_<YYYY-MM-DD>`` (``Case.dirname``,
    scripts/ross_replay_bench.py) because ``:`` is illegal in a Windows filename. A
    directory that does not match that shape is REPORTED and skipped rather than guessed
    at — ``bench.json`` and any other file at the root of the out-dir land here too.
    """
    if not os.path.isdir(run_dir):
        raise SystemExit(
            f"[rossbench_export_recorded_events] --run-dir {run_dir!r} is not a directory"
        )
    out: list[tuple[str, str, str]] = []
    skipped: list[str] = []
    for name in sorted(os.listdir(run_dir)):
        if not os.path.isdir(os.path.join(run_dir, name)):
            continue
        m = _CASE_DIR_RE.match(name)
        if not m:
            skipped.append(name)
            continue
        # Carry the REAL directory name: it is where the reporter will look, and for a
        # disambiguated case it is the only place the selector survives.
        out.append((m.group("symbol").upper(), m.group("date"), name))
    for name in skipped:
        logger.warning(
            "[rossbench_export_recorded_events] %r is not a <SYMBOL>[@selector]_<YYYY-MM-DD> "
            "case directory — skipped", name,
        )
    return out


def cases_from_manifest(manifest: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """Distinct ``(symbol, date)`` from ``chili.ross_ground_truth_manifest.v1``.

    The manifest's 4th layer emits one window per ledger leg, so a symbol-day appears
    several times; the recorded side is per symbol-day, not per window, so the pairs are
    de-duplicated here and the caller exports each day once.
    """
    rows = manifest.get("windows") if isinstance(manifest, Mapping) else None
    out: list[tuple[str, str, str]] = []
    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        date = str(row.get("date") or "").strip()
        if not symbol or not _DATE_RE.match(date):
            continue
        # ``out`` holds 3-tuples now, so the membership test must compare the identity
        # pair only — comparing against the whole tuple would never match and every
        # repeated symbol-day would be exported twice.
        if (symbol, date) not in {(a, b) for a, b, _ in out}:
            # No selector on this route, so the directory is the plain form; a caller who
            # needs a disambiguated case passes --run-dir, where the name carries it.
            out.append((symbol, date, f"{symbol}_{date}"))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# ET DAY -> NAIVE-UTC BOUNDS
# ─────────────────────────────────────────────────────────────────────────────

def _et_zone():
    """The ET tzinfo. Raises rather than falling back to a fixed offset.

    A hard-coded -4h/-5h would put the day boundary in the wrong place on either side of a
    DST switch, and an export bounded to the wrong day is worse than a refused one — the
    same reasoning ross_manifest_adapter._et_zone gives for its own refusal.
    """
    from zoneinfo import ZoneInfo  # noqa: PLC0415 - optional dep, imported where used

    return ZoneInfo(ET_ZONE_NAME)


def et_day_bounds_utc(date: str) -> tuple[datetime, datetime]:
    """ET trading day -> ``[lo, hi)`` as NAIVE UTC datetimes.

    Naive because ``trading_automation_events.ts`` is a naive ``DateTime`` column
    (app/models/trading.py:4061, defaulted from ``datetime.utcnow``); binding a tz-aware
    bound against it would coerce through the session zone.

    The FULL ET day is exported, not just the grading window. The scorer filters to the
    window itself (``events_in_window``), so a wider export lets a window be re-derived —
    from a different pin, say — without re-reading the live database, and the ledger's own
    mechanism prose routinely cites events an hour outside the traded window.
    """
    if not _DATE_RE.match(str(date or "")):
        raise ValueError(f"date must be YYYY-MM-DD, got {date!r}")
    zone = _et_zone()
    day = datetime.fromisoformat(f"{date}T00:00:00").replace(tzinfo=zone)
    nxt = (day + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    # Re-localise the next midnight so a DST transition gives a 23h or 25h day, not 24h.
    nxt = datetime.fromisoformat(f"{nxt.date().isoformat()}T00:00:00").replace(tzinfo=zone)
    from datetime import timezone as _tz  # noqa: PLC0415

    return (day.astimezone(_tz.utc).replace(tzinfo=None),
            nxt.astimezone(_tz.utc).replace(tzinfo=None))


# ─────────────────────────────────────────────────────────────────────────────
# PAYLOAD PARITY WITH THE REPLAY SIDE
# ─────────────────────────────────────────────────────────────────────────────

def bench_payload_keys(driver_source: str = DRIVER_SOURCE) -> tuple[str, ...]:
    """``_BENCH_PAYLOAD_KEYS`` read out of the driver SOURCE with ``ast``.

    Not imported: scripts/replay_v3_fsm_window.py raises ``SystemExit`` at import time
    unless ``TEST_DATABASE_URL`` names a ``_test`` database (:145-149), so importing it
    from a reporting tool would either abort or require pointing this read-only exporter at
    a sink DSN it has no business knowing about.

    Not regex-matched either: a regex over source is exactly the rot this project has been
    bitten by before (reference_source_guard_windows_rot). ``ast.literal_eval`` on the
    assignment's value node either yields the real tuple or raises.
    """
    try:
        with open(driver_source, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=driver_source)
    except (OSError, SyntaxError, ValueError) as exc:
        raise SystemExit(
            f"[rossbench_export_recorded_events] could not parse {driver_source}: {exc}. "
            "The payload allow-list cannot be read, and exporting a payload shape the "
            "replay side does not carry would grade the two sides differently."
        )
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "_BENCH_PAYLOAD_KEYS":
                return tuple(str(k) for k in ast.literal_eval(node.value))
    raise SystemExit(
        "[rossbench_export_recorded_events] could not find _BENCH_PAYLOAD_KEYS in "
        f"{driver_source} — the driver's payload allow-list moved or was renamed. Fix this "
        "rather than exporting a payload shape the replay side does not carry."
    )


def _load_bearing_fn() -> Callable[[str, dict], dict]:
    """``_load_bearing_payload`` from the parity-fixture exporter, imported not copied.

    A second copy of that allow-list would drift, and a drifted recorded payload is graded
    against a replay payload it no longer matches. Imported lazily because that module pulls
    ``psycopg2`` at module scope and the pure functions here must stay importable without a
    database driver.
    """
    from scripts import export_replay_v3_parity_fixtures as fx  # noqa: PLC0415

    return fx._load_bearing_payload


def bench_payload(
    event_type: str,
    payload: Any,
    *,
    keys: Sequence[str],
    load_bearing: Optional[Callable[[str, dict], dict]] = None,
) -> dict:
    """The payload the replay receipt would have carried for this event.

    Reproduces ``_bench_payload`` (scripts/replay_v3_fsm_window.py:739-745): the parity
    fixture's load-bearing set, then the bench keys layered on top. ``load_bearing`` is
    injectable so this function is testable without importing psycopg2.
    """
    p = payload if isinstance(payload, Mapping) else {}
    fn = load_bearing or _load_bearing_fn()
    keep = dict(fn(str(event_type), dict(p)))
    for k in keys:
        if k in p:
            keep[k] = p[k]
    return keep


def event_row(
    ts: Any,
    event_type: Any,
    payload: Any,
    session_id: Any,
    mode: Any,
    *,
    keys: Sequence[str],
    full_payload: bool = False,
    load_bearing: Optional[Callable[[str, dict], dict]] = None,
) -> dict:
    """One exported line, in the shape both the scorer and the driver receipt use."""
    raw = payload
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    if not isinstance(raw, Mapping):
        raw = {}
    return {
        "ts": (ts.isoformat() if isinstance(ts, datetime) else (None if ts is None else str(ts))),
        "event_type": str(event_type),
        "payload": (dict(raw) if full_payload
                    else bench_payload(str(event_type), raw, keys=keys,
                                       load_bearing=load_bearing)),
        "session_id": (int(session_id) if session_id is not None else None),
        "mode": (str(mode) if mode is not None else None),
    }


# ─────────────────────────────────────────────────────────────────────────────
# DATABASE (READ-ONLY)
# ─────────────────────────────────────────────────────────────────────────────

_EVENTS_SQL = (
    "SELECT e.ts, e.event_type, e.payload_json, e.session_id, s.mode "
    "FROM trading_automation_events e "
    "JOIN trading_automation_sessions s ON s.id = e.session_id "
    "WHERE s.symbol = %s AND e.ts >= %s AND e.ts < %s{mode} "
    "ORDER BY e.ts ASC, e.id ASC"
)

# Counted regardless of the --mode filter so an export that came back empty can say whether
# the symbol-day had no sessions at all or only sessions in a mode that was filtered out.
_MODES_SEEN_SQL = (
    "SELECT s.mode, count(*) "
    "FROM trading_automation_events e "
    "JOIN trading_automation_sessions s ON s.id = e.session_id "
    "WHERE s.symbol = %s AND e.ts >= %s AND e.ts < %s "
    "GROUP BY s.mode ORDER BY s.mode"
)


def resolve_dsn(cli_dsn: Optional[str] = None) -> str:
    """DSN for the RECORDED lane, with no invented default.

    The recorded side lives in the live database — the same one
    scripts/nightly_replay_report.py:23 names ``postgresql://chili:chili@localhost:5433/chili``
    — but this tool will not reach for it on its own. Silently connecting to production
    because an env var happened to be unset is how a read-only tool becomes an incident.
    """
    dsn = (cli_dsn or "").strip() or (os.environ.get("DATABASE_URL") or "").strip()
    if not dsn:
        raise SystemExit(
            "[rossbench_export_recorded_events] no DSN: pass --dsn or set DATABASE_URL. "
            "The recorded lane lives in the live database (the canonical local form is "
            "postgresql://chili:chili@localhost:5433/chili, "
            "scripts/nightly_replay_report.py:23); this tool will not guess it."
        )
    return dsn


def dsn_database(dsn: str) -> str:
    """The parsed DATABASE NAME, never a substring of the URL.

    A suffix test on the whole URL passes ``.../chili?application_name=chili_hydrated``,
    which connects to PROD — the trap scripts/replay_v3_fsm_window.py:132 documents.
    """
    return dsn.rpartition("/")[2].partition("?")[0]


def open_readonly_conn(dsn: str):
    """Read-only connection with the session pinned to UTC."""
    import psycopg2  # noqa: PLC0415

    conn = psycopg2.connect(dsn)
    conn.set_session(readonly=True)
    with conn.cursor() as cur:
        cur.execute("SET TIME ZONE 'UTC'")
    conn.commit()
    return conn


def fetch_symbol_day(
    conn,
    symbol: str,
    date: str,
    *,
    modes: Sequence[str] = DEFAULT_MODES,
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
) -> dict:
    """Every automation event for one symbol-day. Read-only; rolled back afterwards.

    Returns ``{"rows": [...], "lo": dt, "hi": dt, "modes_seen": {...}}`` with the raw DB
    tuples in ``rows``; shaping is the caller's job so the pure row builder stays testable.

    The transaction is rolled back immediately so ``query_start`` stays fresh — the same
    discipline ``rossbench_pin_ross_events.fetch_tape_slice`` applies for the db_watchdog.
    (Both pin-tool citations are by SYMBOL: that file is under active edit.)
    """
    lo, hi = et_day_bounds_utc(date)
    want_all = MODE_ALL in {str(m).strip().lower() for m in modes}
    sql = _EVENTS_SQL.format(mode="" if want_all else " AND s.mode = ANY(%s)")
    args: list[Any] = [symbol, lo, hi]
    if not want_all:
        args.append([str(m) for m in modes])
    rows: list[tuple] = []
    modes_seen: dict[str, int] = {}
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = %s",
                        (f"{int(statement_timeout_ms)}ms",))
            cur.execute(_MODES_SEEN_SQL, (symbol, lo, hi))
            modes_seen = {str(m): int(n) for m, n in cur.fetchall()}
            cur.execute(sql, tuple(args))
            rows = list(cur.fetchall())
    finally:
        conn.rollback()
    return {"rows": rows, "lo": lo, "hi": hi, "modes_seen": modes_seen}


# ─────────────────────────────────────────────────────────────────────────────
# WRITING
# ─────────────────────────────────────────────────────────────────────────────

def write_jsonl(path: str, rows: Iterable[Mapping[str, Any]]) -> int:
    """One JSON object per line, LF-only.

    ``newline="\\n"``: Windows text mode rewrites every ``\\n`` to ``\\r\\n``, which changes
    the bytes of an otherwise identical export (reference_python_write_text_crlf_windows).
    """
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    written = 0
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")
            written += 1
    return written


def write_json(path: str, doc: Mapping[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(doc, handle, indent=2, default=str, ensure_ascii=False)
        handle.write("\n")


def case_meta(
    symbol: str,
    date: str,
    *,
    database: str,
    lo: datetime,
    hi: datetime,
    rows: Sequence[Mapping[str, Any]],
    modes_requested: Sequence[str],
    modes_seen: Mapping[str, int],
    payload_filter: str,
    payload_keys: Sequence[str],
) -> dict:
    """Provenance for one case's export.

    The load-bearing field is ``modes_seen``: an empty ``recorded_events.jsonl`` next to
    ``modes_seen: {"paper": 412}`` says "the live lane was absent, the paper lane was not",
    which is a finding. The same empty file with ``modes_seen: {}`` says the symbol-day has
    no automation events at all. Those are different facts and the export must not collapse
    them into one blank column.
    """
    sessions = sorted({r.get("session_id") for r in rows if r.get("session_id") is not None})
    by_type: dict[str, int] = {}
    for r in rows:
        key = str(r.get("event_type"))
        by_type[key] = by_type.get(key, 0) + 1
    return {
        "schema": EXPORT_SCHEMA,
        "symbol": symbol,
        "date": date,
        "database": database,
        "window_utc_naive": [lo.isoformat(), hi.isoformat()],
        "window_basis": (
            f"full {ET_ZONE_NAME} trading day; the scorer filters to the grading window "
            "itself (ross_bench_scoring.events_in_window)"
        ),
        "event_count": len(rows),
        "session_ids": sessions,
        "session_count": len(sessions),
        "sessions_merged_note": (
            "events from every matching session are merged into ONE chronological stream: "
            "the bench asks whether the live lane reached a rung for this symbol-day, not "
            "whether one particular session did. session_id is on every row so a reader can "
            "split them again."
        ),
        "modes_requested": list(modes_requested),
        "modes_seen_in_window": dict(modes_seen),
        "event_type_counts": dict(sorted(by_type.items())),
        "payload_filter": payload_filter,
        "payload_keys": list(payload_keys),
        "payload_parity_note": (
            "payloads are filtered by the same allow-list the driver applies to its own "
            "receipt (_load_bearing_payload UNION _BENCH_PAYLOAD_KEYS), so the recorded and "
            "replay sides are graded on one payload shape. 'detector_rejects' is dropped by "
            "that allow-list on BOTH sides, which is why the scorer's "
            "detector_rejects_present diagnostic reads false unless --full-payload was used."
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--run-dir", default=None,
                    help="bench out-dir; case dirs <SYMBOL>_<YYYY-MM-DD> select the cases "
                         "AND receive the export (default --out-dir)")
    ap.add_argument("--out-dir", default=None,
                    help="where <case>/recorded_events.jsonl is written (default: --run-dir)")
    ap.add_argument("--cases", default=None, metavar="SYM:DATE,...",
                    help="explicit case list; overrides --run-dir/--manifest discovery")
    ap.add_argument("--manifest", default=None,
                    help="chili.ross_ground_truth_manifest.v1 to take symbol-days from")
    ap.add_argument("--dsn", default=None,
                    help="DSN for the recorded lane (default: DATABASE_URL; no built-in default)")
    ap.add_argument("--mode", action="append", default=[],
                    help=f"session mode(s) to export; repeatable. Default {list(DEFAULT_MODES)}; "
                         f"pass {MODE_ALL!r} for no filter.")
    ap.add_argument("--statement-timeout-ms", type=int, default=DEFAULT_STATEMENT_TIMEOUT_MS,
                    help="per-statement fence (default: %(default)s)")
    ap.add_argument("--full-payload", action="store_true",
                    help="keep the whole payload instead of the driver's allow-list. Breaks "
                         "payload parity with the replay side; recorded in the meta.")
    ap.add_argument("--overwrite", action="store_true",
                    help="replace an existing recorded_events.jsonl (default: refuse)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan and the resolved bounds; open no connection")
    args = ap.parse_args(argv)

    out_dir = args.out_dir or args.run_dir
    if not out_dir:
        raise SystemExit(
            "[rossbench_export_recorded_events] pass --run-dir (and/or --out-dir): the "
            "export is written beside the run tree the reporter reads"
        )

    if args.cases:
        cases = parse_cases(args.cases)
        case_source = "--cases"
    elif args.manifest:
        with open(args.manifest, encoding="utf-8") as handle:
            cases = cases_from_manifest(json.load(handle))
        case_source = f"--manifest {args.manifest}"
    elif args.run_dir:
        cases = cases_from_run_dir(args.run_dir)
        case_source = f"--run-dir {args.run_dir}"
    else:
        raise SystemExit(
            "[rossbench_export_recorded_events] nothing to export: pass --cases, "
            "--manifest, or a --run-dir containing <SYMBOL>_<YYYY-MM-DD> case directories"
        )
    if not cases:
        raise SystemExit(
            f"[rossbench_export_recorded_events] {case_source} selected 0 symbol-days"
        )

    modes = tuple(args.mode) if args.mode else DEFAULT_MODES
    keys = bench_payload_keys()
    payload_filter = ("full_payload" if args.full_payload
                      else "_load_bearing_payload + _BENCH_PAYLOAD_KEYS")

    if args.dry_run:
        for symbol, date, case_dirname in cases:
            lo, hi = et_day_bounds_utc(date)
            logger.info("[rossbench_export_recorded_events] %s %s -> %s .. %s (naive UTC)",
                        symbol, date, lo.isoformat(), hi.isoformat())
        logger.info("[rossbench_export_recorded_events] %d case(s) from %s; modes=%s; "
                    "payload_filter=%s; no connection opened",
                    len(cases), case_source, list(modes), payload_filter)
        return 0

    dsn = resolve_dsn(args.dsn)
    database = dsn_database(dsn)
    load_bearing = _load_bearing_fn()
    conn = open_readonly_conn(dsn)
    logger.info("[rossbench_export_recorded_events] read-only on %s; %d case(s) from %s; "
                "modes=%s", database, len(cases), case_source, list(modes))

    exported = 0
    empty: list[str] = []
    skipped: list[str] = []
    try:
        for symbol, date, case_dirname in cases:
            # The runner's own directory name, so a disambiguated case lands where
            # the reporter reads it instead of in a sibling it never opens.
            case_dir = os.path.join(out_dir, case_dirname)
            events_path = os.path.join(case_dir, EVENTS_FILENAME)
            if os.path.exists(events_path) and not args.overwrite:
                skipped.append(f"{symbol}_{date}")
                logger.warning(
                    "[rossbench_export_recorded_events] %s exists; pass --overwrite to "
                    "replace it", events_path,
                )
                continue
            fetched = fetch_symbol_day(
                conn, symbol, date, modes=modes,
                statement_timeout_ms=args.statement_timeout_ms,
            )
            rows = [
                event_row(ts, et, pl, sid, mode, keys=keys,
                          full_payload=args.full_payload, load_bearing=load_bearing)
                for ts, et, pl, sid, mode in fetched["rows"]
            ]
            n = write_jsonl(events_path, rows)
            write_json(os.path.join(case_dir, META_FILENAME), case_meta(
                symbol, date, database=database, lo=fetched["lo"], hi=fetched["hi"],
                rows=rows, modes_requested=modes, modes_seen=fetched["modes_seen"],
                payload_filter=payload_filter, payload_keys=keys,
            ))
            exported += 1
            if n == 0:
                empty.append(f"{symbol}_{date}")
            logger.info("[rossbench_export_recorded_events] %s_%s events=%d sessions=%d "
                        "modes_seen=%s", symbol, date, n,
                        len({r["session_id"] for r in rows if r["session_id"] is not None}),
                        fetched["modes_seen"])
    finally:
        conn.close()

    logger.info("[rossbench_export_recorded_events] wrote %d case export(s) under %s "
                "(%d empty, %d skipped)", exported, os.path.abspath(out_dir),
                len(empty), len(skipped))
    if empty:
        # Not an error. An empty recorded side is a real answer — it is what
        # 'no live session in this window' looks like — and the meta says which kind.
        logger.info("[rossbench_export_recorded_events] empty (see the meta for modes "
                    "seen): %s", ", ".join(empty))
    return 0


if __name__ == "__main__":
    sys.exit(main())
