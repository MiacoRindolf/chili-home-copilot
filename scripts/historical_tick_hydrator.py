"""Historical tick/NBBO hydrator — buy the history we did not record.

THE PROBLEM THIS SOLVES
-----------------------
``iqfeed_trade_ticks`` and ``momentum_nbbo_spread_tape`` only ever contain
symbols the live bridge happened to be SUBSCRIBED to at the time.  A sibling
coverage study found 307 in-band movers we never discovered, and therefore have
no tick or NBBO history for.  That is not a feasibility wall: we pay for IQFeed
and hold live Massive/Polygon credentials, and Phase 1 proved every one of those
symbol-days is a request away.  This module turns that entitlement into rows.

The deliverable is INFRASTRUCTURE, not a one-off: any symbol on any past date
within provider retention becomes drivable through the real FSM, because the
rows land in the SAME tables, with the SAME column semantics, that
``momentum_neural/counterfactual_replay.py`` already reads.

PROVENANCE — HYDRATED DATA CAN NEVER BE MISTAKEN FOR RECORDED DATA
------------------------------------------------------------------
This is enforced by the EXISTING replay query, not by convention.
``counterfactual_replay.load_trade_tape``/``load_nbbo_tape`` accept a
``require_causal_provenance`` flag whose strict predicate demands, among others:

    source = 'iqfeed_l1' AND provider_event_at IS NULL AND available_at IS NOT NULL

Every hydrated row violates all three, independently:

  1. ``source`` is one of the four hydrated values below — never ``iqfeed_l1``.
  2. ``provider_event_at`` is NOT NULL (we know the provider's event clock).
  3. ``available_at`` is NULL, and deliberately so: for hydrated data there is
     no honest answer to "when would the live lane have first seen this row".
     Fabricating one would be the single most dangerous thing this module could
     do, because it is the clock strict causal replay trusts.

So hydrated rows are visible to ordinary (non-strict) replay and INVISIBLE to
strict causal replay.  That is the correct default in both directions.

Per-row provenance additionally rides in columns that already exist:
  ``bridge_run_id``        = the hydration batch UUID (joins ``hydration_batches``)
  ``bridge_version``       = HYDRATOR_VERSION
  ``timestamp_basis``      = which provider clock the timestamp came from
  ``message_type``         = 'H' (historical/hydrated, vs the live bridge's 'Q')
  ``source_frame_sha256``  = sha256 of the exact provider record bytes
  ``source_frame_sequence``= the provider's own sequence (IQFeed tickid /
                             Polygon sequence_number)
No schema change was needed for any of it.

THE TIMEZONE LANDMINE (Phase 1's most consequential finding)
------------------------------------------------------------
``iqfeed_trade_ticks.observed_at`` is ``TIMESTAMP WITHOUT TIME ZONE`` holding
UTC.  The IQFeed LOOKUP port returns ET-naive timestamps.  Writing lookup
timestamps straight through shifts every row by 4 or 5 hours and the rows still
look perfectly well-formed.  The offset is NOT constant — DST began 2026-03-08
while retention reaches back to 2026-03-06, so the two oldest retrievable days
are EST and everything after is EDT.  Conversion therefore goes through
``zoneinfo("America/New_York")``, never a fixed offset.  And the two target
tables disagree with each other: the tick table is WITHOUT time zone, the NBBO
tape is WITH.  ``_naive_utc`` / aware datetimes below are not stylistic.

IQFEED TRUNCATION SEMANTICS (measured, not assumed)
---------------------------------------------------
When an HTT response is capped by MaxDatapoints, IQFeed keeps the NEWEST N
records and drops the OLDEST — regardless of DataDirection, which only controls
the order of the lines it sends.  Proof: a 1,670-tick window requested with
MaxDatapoints=835 returned, under BOTH directions, the records ending at the
window's last tick and starting mid-window.  A capped window is therefore
CONTINUED BACKWARD (re-request ``[begin, oldest_returned]``), not bisected.
Note also that HTT bounds are INCLUSIVE at second resolution, so tiled windows
end one second before the next begins or the shared second is double-counted.

TWO OPERATIONAL INTERLOCKS (both added after review; both enforced in code)
--------------------------------------------------------------------------
1. ONE HYDRATOR AT A TIME, MACHINE-WIDE.  IQFeed limits SIMULTANEOUS lookup
   connections, and the failure mode when that limit is exceeded is unknown and
   plausibly affects the SHARED IQConnect process the live L1 bridge depends on
   -- so it was never tested deliberately.  Two operators (or two agent
   sessions, which is this project's demonstrated operating mode) each running
   ``--provider iqfeed`` would each open their own :9100 socket.  A Postgres
   session-level advisory lock on the hydrated database prevents that: the tool
   already holds a connection, the lock dies with the session so a crashed run
   cannot wedge it, and it is machine-wide.  See ``singleton_lock``.

2. NOT DURING MARKET HOURS, UNLESS SAID OUT LOUD.  ``chili_hydrated`` is a
   SEPARATE DATABASE but NOT a separate cluster: one postmaster, one WAL, one
   4 GB shared_buffers, one bind mount, shared with the 222 GB live ``chili``.
   Phase 4's clean run depended on 98% of its 35.5M rows landing after the
   20:00 ET close -- that was scheduling luck, not a property of the tool.
   ``assert_outside_market_hours`` makes it a property of the tool.

Usage
-----
  # one-time: create the hydration database and its schema
  python scripts/historical_tick_hydrator.py --init-db

  # hydrate specific symbol-days
  python scripts/historical_tick_hydrator.py --provider iqfeed \\
      --symbol-day CANF:2026-09-02 --symbol-day SSM:2026-09-01

  # hydrate a corpus from CSV (columns: symbol,date)
  python scripts/historical_tick_hydrator.py --provider iqfeed --csv corpus.csv

  # what is loaded / what failed
  python scripts/historical_tick_hydrator.py --status
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import logging
import os
import sys
import time
import uuid
from array import array
from bisect import bisect_right
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from datetime import time as time_
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras

_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:
    from scripts.iqfeed_lookup_client import (
        ByteCapExceeded,
        IQFeedLookupClient,
        RequestResult,
    )
    from scripts.polygon_historical_client import (
        FetchStats,
        PolygonHistoricalClient,
        env_file_candidates,
    )
except ModuleNotFoundError:  # direct ``python scripts/...`` host invocation
    from iqfeed_lookup_client import (  # type: ignore[no-redef]
        ByteCapExceeded,
        IQFeedLookupClient,
        RequestResult,
    )
    from polygon_historical_client import (  # type: ignore[no-redef]
        FetchStats,
        PolygonHistoricalClient,
        env_file_candidates,
    )

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("historical_tick_hydrator")

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

HYDRATOR_VERSION = "chili-historical-hydrator/1.0.0"

# ``source`` is VARCHAR(24) on both tables. Every value below fits, and none
# collides with a recorded source ('iqfeed_l1', 'massive_snapshot',
# 'massive_ws_universe'), which is what keeps hydrated rows out of the strict
# causal-provenance predicate and out of the live BBO read in live_runner.py.
SOURCE_IQFEED_TRADES = "iqfeed_lookup_hist"
SOURCE_IQFEED_NBBO = "iqfeed_lookup_bbo"
SOURCE_POLYGON_TRADES = "polygon_v3_trades"
SOURCE_POLYGON_NBBO = "polygon_v3_quotes"
HYDRATED_SOURCES = (
    SOURCE_IQFEED_TRADES,
    SOURCE_IQFEED_NBBO,
    SOURCE_POLYGON_TRADES,
    SOURCE_POLYGON_NBBO,
)

# ``timestamp_basis`` is VARCHAR(48).
BASIS_IQFEED = "hydrated_iqfeed_lookup_trade_time_et"
BASIS_POLYGON = "hydrated_polygon_sip_timestamp_ns"

MESSAGE_TYPE_HYDRATED = "H"

TRADES_TABLE = "iqfeed_trade_ticks"
NBBO_TABLE = "momentum_nbbo_spread_tape"

# Measured Phase 1: retention is a clean 180-calendar-day cliff. 2026-03-06
# returned data; every weekday from 2026-02-23 through 2026-03-05 returned
# NO_DATA. The boundary moves forward one day per day, so it is computed, not
# frozen — but the constant records what was actually observed.
IQFEED_RETENTION_DAYS = 180
IQFEED_RETENTION_MEASURED_ON = date(2026, 9, 2)
IQFEED_RETENTION_FLOOR_MEASURED = date(2026, 3, 6)

# One HTT may not exceed these. Phase 1 found no provider ceiling below 500,000
# records / ~50 MB, so both of these are OUR limits, chosen so a single window
# cannot balloon the shared IQConnect process or this process's memory.
IQFEED_MAX_DATAPOINTS = 200_000
IQFEED_BYTE_CAP = 64 * 1024 * 1024
IQFEED_TIMEOUT_S = 120.0
# Ticks are buffered per window so they can be sorted before COPY. One hour of a
# low-float mover is a few tens of thousands of rows; this bounds the buffer
# without forcing a request per minute.
IQFEED_WINDOW_MINUTES = 60
# Refuse to spin forever if a window keeps coming back truncated.
IQFEED_MAX_CONTINUATIONS = 64

DEFAULT_HYDRATED_DB = "chili_hydrated"


# ---------------------------------------------------------------------------
# row shapes
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class TradeTick:
    ts_utc: datetime          # aware UTC; the provider's event clock
    price: float
    size: float
    bid: float | None
    ask: float | None
    day_volume: float | None
    sequence: int | None
    frame_sha256: str


@dataclass(slots=True)
class QuoteTick:
    ts_utc: datetime
    bid: float
    ask: float
    day_volume: float | None
    sequence: int | None
    frame_sha256: str


@dataclass
class HydrationResult:
    symbol: str
    trading_day: date
    provider: str
    status: str = "pending"
    trade_rows: int = 0
    nbbo_rows: int = 0
    requests: int = 0
    bytes_received: int = 0
    wall_s: float = 0.0
    batch_id: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        d = {
            "symbol": self.symbol,
            "trading_day": self.trading_day.isoformat(),
            "provider": self.provider,
            "status": self.status,
            "trade_rows": self.trade_rows,
            "nbbo_rows": self.nbbo_rows,
            "requests": self.requests,
            "bytes_received": self.bytes_received,
            "wall_s": round(self.wall_s, 3),
            "batch_id": self.batch_id,
        }
        if self.error:
            d["error"] = self.error
        return d


# ---------------------------------------------------------------------------
# time helpers — the landmine lives here, so it lives in ONE place
# ---------------------------------------------------------------------------
def et_naive_to_utc(text: str) -> datetime:
    """Parse an IQFeed lookup timestamp (ET-naive) into an aware UTC datetime.

    IQFeed lookup emits ``YYYY-MM-DD HH:MM:SS[.ffffff]`` in US/Eastern with NO
    zone marker.  Converting with a fixed -4h offset is wrong for any date
    before the 2026-03-08 DST transition, which is inside retention.  ``fold=0``
    resolves the one ambiguous hour on the autumn transition; US equity sessions
    (04:00-20:00 ET) never overlap it, so the choice is defensive only.
    """
    text = text.strip()
    fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in text else "%Y-%m-%d %H:%M:%S"
    naive = datetime.strptime(text, fmt)
    return naive.replace(tzinfo=ET, fold=0).astimezone(UTC)


def naive_utc(ts: datetime) -> datetime:
    """Strip the zone from an aware UTC datetime.

    ``iqfeed_trade_ticks.observed_at`` is TIMESTAMP WITHOUT TIME ZONE holding
    UTC (proved in Phase 1: observed_at-read-as-UTC minus provider_event_at is
    exactly 0.0s on every sampled recorded row).  The NBBO tape's ``observed_at``
    is TIMESTAMPTZ and must NOT go through this.
    """
    return ts.astimezone(UTC).replace(tzinfo=None)


def et_day_bounds_utc(day: date) -> tuple[datetime, datetime]:
    """[start, end) of one ET calendar day, as aware UTC datetimes."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=ET).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=ET).astimezone(UTC)
    return start, end


def iqfeed_retention_floor(today: date | None = None) -> date:
    """Oldest date IQFeed lookup can still serve ticks for."""
    ref = today or datetime.now(ET).date()
    return ref - timedelta(days=IQFEED_RETENTION_DAYS)


# ---------------------------------------------------------------------------
# COPY FROM STDIN plumbing
# ---------------------------------------------------------------------------
_COPY_ESCAPES = str.maketrans({
    "\\": "\\\\",
    "\t": "\\t",
    "\n": "\\n",
    "\r": "\\r",
})


def copy_field(value: Any) -> str:
    """Render one value in PostgreSQL COPY TEXT format."""
    if value is None:
        return "\\N"
    if isinstance(value, bool):
        return "t" if value else "f"
    if isinstance(value, datetime):
        # isoformat() keeps microseconds and, for aware values, the offset.
        return value.isoformat(sep=" ")
    if isinstance(value, float):
        return repr(value)
    return str(value).translate(_COPY_ESCAPES)


def copy_line(values: Sequence[Any]) -> str:
    return "\t".join(copy_field(v) for v in values) + "\n"


class CopyStream:
    """A read()-able adapter over an iterator of rows, for ``copy_expert``.

    Streaming matters: a busy symbol-day is hundreds of thousands of rows, and
    materialising the whole COPY payload as one string defeats the point of
    using COPY at all.  ``rows_written`` is the authoritative count for the
    batch provenance record — it counts what actually crossed the wire.
    """

    def __init__(self, rows: Iterable[Sequence[Any]], *, chunk_bytes: int = 1 << 20) -> None:
        self._rows = iter(rows)
        self._buf = ""
        self._chunk = chunk_bytes
        self.rows_written = 0
        self.bytes_written = 0
        self._digest = hashlib.sha256()

    @property
    def content_sha256(self) -> str:
        return self._digest.hexdigest()

    def read(self, size: int = -1) -> str:
        if size is None or size < 0:
            size = self._chunk
        while len(self._buf) < size:
            try:
                row = next(self._rows)
            except StopIteration:
                break
            line = copy_line(row)
            self._buf += line
            self.rows_written += 1
            self.bytes_written += len(line)
            self._digest.update(line.encode("utf-8"))
        out, self._buf = self._buf[:size], self._buf[size:]
        return out

    # psycopg2's copy_expert only calls read(), but readline keeps the object
    # honest as a file-like for anything else that touches it.
    def readline(self, size: int = -1) -> str:  # pragma: no cover - unused by psycopg2
        while "\n" not in self._buf:
            try:
                row = next(self._rows)
            except StopIteration:
                break
            line = copy_line(row)
            self._buf += line
            self.rows_written += 1
            self.bytes_written += len(line)
            self._digest.update(line.encode("utf-8"))
        if "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            return line + "\n"
        out, self._buf = self._buf, ""
        return out


TRADE_COLUMNS = (
    "symbol", "observed_at", "price", "size", "bid", "ask", "source",
    "provider_event_at", "received_at", "timestamp_basis", "bridge_version",
    "provider_trade_reference_at", "message_type", "bridge_run_id",
    "source_frame_sequence", "source_frame_sha256",
)
# available_at and connection_generation are deliberately absent: they default
# to NULL, and NULL is the honest value. See the module docstring.

NBBO_COLUMNS = (
    "symbol", "observed_at", "bid", "ask", "mid", "spread_bps", "day_volume",
    "source", "provider_event_at", "received_at", "timestamp_basis",
    "bridge_version", "provider_trade_reference_at", "message_type",
    "bridge_run_id", "source_frame_sequence", "source_frame_sha256",
)


def trade_copy_row(
    symbol: str, tick: TradeTick, *, source: str, basis: str,
    batch_id: str, received_at: datetime,
) -> tuple[Any, ...]:
    return (
        symbol,
        naive_utc(tick.ts_utc),   # TIMESTAMP WITHOUT TIME ZONE, holding UTC
        tick.price,
        tick.size,
        tick.bid,
        tick.ask,
        source,
        tick.ts_utc,              # provider_event_at  (TIMESTAMPTZ, aware)
        received_at,
        basis,
        HYDRATOR_VERSION,
        tick.ts_utc,              # provider_trade_reference_at
        MESSAGE_TYPE_HYDRATED,
        batch_id,
        tick.sequence,
        tick.frame_sha256,
    )


def nbbo_copy_row(
    symbol: str, quote: QuoteTick, *, source: str, basis: str,
    batch_id: str, received_at: datetime,
) -> tuple[Any, ...]:
    mid = (quote.bid + quote.ask) / 2.0
    spread_bps = ((quote.ask - quote.bid) / mid * 10_000.0) if mid > 0 else None
    return (
        symbol,
        quote.ts_utc,             # TIMESTAMPTZ — aware, NOT stripped
        quote.bid,
        quote.ask,
        mid,
        spread_bps,
        quote.day_volume,
        source,
        quote.ts_utc,
        received_at,
        basis,
        HYDRATOR_VERSION,
        quote.ts_utc,
        MESSAGE_TYPE_HYDRATED,
        batch_id,
        quote.sequence,
        quote.frame_sha256,
    )


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------
# Mirrors the LIVE chili definitions exactly (verified read-only against
# postgres 16.13 on 2026-09-02), because replay reads these tables by name and
# by column. Divergence here is silent corruption downstream.
DDL_TRADES = """
CREATE TABLE IF NOT EXISTS iqfeed_trade_ticks (
    id BIGSERIAL PRIMARY KEY,
    symbol VARCHAR(16) NOT NULL,
    observed_at TIMESTAMP NOT NULL,
    price DOUBLE PRECISION NOT NULL,
    size DOUBLE PRECISION NOT NULL,
    bid DOUBLE PRECISION,
    ask DOUBLE PRECISION,
    source VARCHAR(24) NOT NULL DEFAULT 'iqfeed_l1',
    provider_event_at TIMESTAMPTZ,
    received_at TIMESTAMPTZ,
    timestamp_basis VARCHAR(48),
    bridge_version VARCHAR(96),
    provider_trade_reference_at TIMESTAMPTZ,
    message_type VARCHAR(1),
    bridge_run_id VARCHAR(36),
    connection_generation BIGINT,
    available_at TIMESTAMPTZ,
    source_frame_sequence BIGINT,
    source_frame_sha256 VARCHAR(64)
)
"""

DDL_NBBO = """
CREATE TABLE IF NOT EXISTS momentum_nbbo_spread_tape (
    id BIGSERIAL PRIMARY KEY,
    symbol VARCHAR(32) NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    bid DOUBLE PRECISION,
    ask DOUBLE PRECISION,
    mid DOUBLE PRECISION,
    spread_bps DOUBLE PRECISION,
    day_volume DOUBLE PRECISION,
    source VARCHAR(24) NOT NULL DEFAULT 'massive_snapshot',
    provider_event_at TIMESTAMPTZ,
    received_at TIMESTAMPTZ,
    timestamp_basis VARCHAR(48),
    bridge_version VARCHAR(96),
    provider_trade_reference_at TIMESTAMPTZ,
    message_type VARCHAR(1),
    bridge_run_id VARCHAR(36),
    connection_generation BIGINT,
    available_at TIMESTAMPTZ,
    source_frame_sequence BIGINT,
    source_frame_sha256 VARCHAR(64)
)
"""

DDL_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_iqfeed_trades_sym_at "
    "ON iqfeed_trade_ticks (symbol, observed_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_iqfeed_trades_at_brin "
    "ON iqfeed_trade_ticks USING brin (observed_at)",
    "CREATE INDEX IF NOT EXISTS ix_nbbo_tape_symbol_observed "
    "ON momentum_nbbo_spread_tape (symbol, observed_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_nbbo_tape_observed "
    "ON momentum_nbbo_spread_tape (observed_at)",
    "CREATE INDEX IF NOT EXISTS ix_nbbo_tape_source_symbol_observed "
    "ON momentum_nbbo_spread_tape (source, symbol, observed_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_nbbo_pending_release "
    "ON momentum_nbbo_spread_tape (bridge_run_id, connection_generation, source_frame_sequence) "
    "WHERE available_at IS NULL AND bridge_run_id IS NOT NULL",
    # Hydration-specific: the idempotency DELETE is by (symbol, source, day).
    "CREATE INDEX IF NOT EXISTS ix_iqfeed_trades_source_sym_at "
    "ON iqfeed_trade_ticks (source, symbol, observed_at)",
)

DDL_LEDGER = (
    # Resumability. One row per (symbol, day, dataset, provider); the PK IS the
    # resume key, so a re-run skips what is already 'done'.
    """
    CREATE TABLE IF NOT EXISTS hydration_jobs (
        symbol VARCHAR(32) NOT NULL,
        trading_day DATE NOT NULL,
        dataset VARCHAR(16) NOT NULL,
        provider VARCHAR(16) NOT NULL,
        status VARCHAR(16) NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        rows_loaded BIGINT NOT NULL DEFAULT 0,
        last_error TEXT,
        first_started_at TIMESTAMPTZ,
        last_finished_at TIMESTAMPTZ,
        last_batch_id VARCHAR(36),
        PRIMARY KEY (symbol, trading_day, dataset, provider)
    )
    """,
    # Immutable per-batch provenance. Every row in the tape tables carries this
    # batch's UUID in bridge_run_id, so any row can be traced to the exact
    # request that produced it.
    """
    CREATE TABLE IF NOT EXISTS hydration_batches (
        batch_id VARCHAR(36) PRIMARY KEY,
        provider VARCHAR(16) NOT NULL,
        dataset VARCHAR(16) NOT NULL,
        table_name VARCHAR(48) NOT NULL,
        symbol VARCHAR(32) NOT NULL,
        trading_day DATE NOT NULL,
        source VARCHAR(24) NOT NULL,
        timestamp_basis VARCHAR(48) NOT NULL,
        hydrator_version VARCHAR(96) NOT NULL,
        requested_at TIMESTAMPTZ NOT NULL,
        completed_at TIMESTAMPTZ NOT NULL,
        window_start_utc TIMESTAMPTZ NOT NULL,
        window_end_utc TIMESTAMPTZ NOT NULL,
        rows_loaded BIGINT NOT NULL,
        rows_deleted BIGINT NOT NULL,
        request_count INTEGER NOT NULL,
        bytes_received BIGINT NOT NULL,
        payload_sha256 VARCHAR(64) NOT NULL,
        provider_request JSONB NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_hydration_batches_sym_day "
    "ON hydration_batches (symbol, trading_day)",
    "CREATE INDEX IF NOT EXISTS ix_hydration_jobs_status "
    "ON hydration_jobs (status, trading_day)",
)


def _admin_dsn(dsn: str, dbname: str = "postgres") -> str:
    head, _, tail = dsn.rpartition("/")
    # Preserve any query string on the original DSN.
    qs = ""
    if "?" in tail:
        _, _, qs = tail.partition("?")
        qs = "?" + qs
    return f"{head}/{dbname}{qs}"


def resolve_dsn(dbname: str = DEFAULT_HYDRATED_DB, env_path: str | None = None) -> str:
    """Build the hydrated-DB DSN from DATABASE_URL, swapping only the db name.

    This NEVER returns a DSN pointing at ``chili``: the hydrated corpus lives in
    its own database, and the live database is read-only to this workflow.
    """
    override = os.environ.get("HYDRATED_DATABASE_URL")
    if override:
        dsn = override
    else:
        base = os.environ.get("DATABASE_URL", "")
        if not base:
            for path in env_file_candidates(env_path):
                for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    if raw.startswith("DATABASE_URL="):
                        base = raw.split("=", 1)[1].strip()
                        break
                if base:
                    break
        if not base:
            raise RuntimeError(
                "DATABASE_URL is not set and no .env entry was found; pass "
                "--env-file or set CHILI_ENV_FILE"
            )
        dsn = _admin_dsn(base, dbname)
    tail = dsn.rpartition("/")[2].partition("?")[0]
    if tail in ("chili", "chili_test"):
        raise RuntimeError(
            f"refusing to hydrate into database {tail!r}: hydrated history must "
            "live in its own database (see --db-name)"
        )
    return dsn


def ensure_database(dbname: str = DEFAULT_HYDRATED_DB, env_path: str | None = None) -> bool:
    """CREATE DATABASE if absent. Returns True when it was created."""
    target = resolve_dsn(dbname, env_path)
    admin = _admin_dsn(target, "postgres")
    conn = psycopg2.connect(admin)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
            if cur.fetchone():
                return False
            cur.execute(f'CREATE DATABASE "{dbname}"')
            return True
    finally:
        conn.close()


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(DDL_TRADES)
        cur.execute(DDL_NBBO)
        for stmt in DDL_INDEXES:
            cur.execute(stmt)
        for stmt in DDL_LEDGER:
            cur.execute(stmt)
    conn.commit()


# ---------------------------------------------------------------------------
# operational interlocks
# ---------------------------------------------------------------------------
# One constant, forever. Changing it silently disables the interlock against
# every already-running hydrator, which is the one thing it exists to prevent.
# (ASCII "CHLHYD!" as a bigint; well inside int8 range.)
HYDRATOR_ADVISORY_LOCK_KEY = 0x43484C48594421


class HydratorAlreadyRunning(RuntimeError):
    """Another hydrator process holds the singleton advisory lock."""


class MarketHoursRefusal(RuntimeError):
    """A load was started inside the 04:00-20:00 ET window without consent."""


def acquire_singleton_lock(conn, key: int = HYDRATOR_ADVISORY_LOCK_KEY) -> bool:
    """Take the machine-wide hydrator lock on ``conn``'s session. Never blocks."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
        got = bool(cur.fetchone()[0])
    conn.commit()
    return got


def release_singleton_lock(conn, key: int = HYDRATOR_ADVISORY_LOCK_KEY) -> None:
    """Best-effort release. The session ending releases it anyway."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
        conn.commit()
    except Exception:  # pragma: no cover - the connection may already be gone
        log.debug("[hydrator] advisory unlock failed (session already closed?)",
                  exc_info=True)


def acquire_singleton_lock_or_raise(
    conn, key: int = HYDRATOR_ADVISORY_LOCK_KEY,
) -> None:
    """Refuse to run a second hydrator against the same hydrated database.

    Session-level, so it is released when this connection closes -- including
    when the process dies -- which means a crashed run can never wedge the next
    one. It is held for the WHOLE load, not per symbol-day, because the resource
    being protected is the shared IQConnect process's simultaneous-lookup-
    connection budget, which is a property of the run, not of a request.
    """
    if acquire_singleton_lock(conn, key):
        return
    raise HydratorAlreadyRunning(
        "another hydrator is running against this database (advisory lock "
        f"{key} is held). IQFeed limits SIMULTANEOUS lookup connections and "
        "the behaviour past that limit is untested against the shared "
        "IQConnect process the live lane depends on, so this refuses rather "
        "than opening a second :9100 socket. Wait for the other run to "
        "finish, or check for a stuck session with: SELECT * FROM pg_locks "
        f"WHERE locktype='advisory' AND objid={key & 0xFFFFFFFF};"
    )


@contextmanager
def singleton_lock(conn, key: int = HYDRATOR_ADVISORY_LOCK_KEY) -> Iterator[None]:
    acquire_singleton_lock_or_raise(conn, key)
    try:
        yield
    finally:
        release_singleton_lock(conn, key)


def assert_outside_market_hours(
    *, allow: bool = False, now: datetime | None = None,
) -> None:
    """Refuse a load inside 04:00-20:00 ET unless the caller said so explicitly.

    The window is the one CHILI actually trades (premarket open through
    postmarket close), and it is applied on EVERY day rather than weekdays only:
    the cluster is shared every day of the week, the exemption flag costs one
    word, and a rule with no calendar edge case is a rule that cannot be got
    wrong. Computed via zoneinfo, never a fixed offset.
    """
    if allow:
        return
    now = (now or datetime.now(timezone.utc))
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    et = now.astimezone(ET)
    if not (time_(4, 0) <= et.time() < time_(20, 0)):
        return
    raise MarketHoursRefusal(
        f"refusing to start a load at {et:%Y-%m-%d %H:%M:%S %Z} — inside the "
        "04:00-20:00 ET session. The hydrated database is a SEPARATE DATABASE "
        "but NOT a separate cluster: one postmaster, one WAL, one 4 GB "
        "shared_buffers and one bind mount (E:/CHILI-Docker/postgres), shared "
        "with the live 222 GB `chili`. A multi-million-row COPY evicts the live "
        "lane's buffers and shares its WAL. Run after 20:00 ET, or pass "
        "--allow-market-hours if you have decided the contention is acceptable."
    )


def connect(dbname: str = DEFAULT_HYDRATED_DB, env_path: str | None = None):
    """Open a hydrated-DB connection with the session pinned to UTC.

    The UTC pin is the same correctness lock the live bridge applies
    (``iqfeed_trade_bridge._set_bridge_session_utc``): naive text written into a
    timestamptz column resolves against the SESSION time zone, and the same
    naive text was measured landing 7 h off under America/Los_Angeles.
    """
    conn = psycopg2.connect(resolve_dsn(dbname, env_path))
    with conn.cursor() as cur:
        cur.execute("SET TIME ZONE 'UTC'")
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# IQFeed lookup adapter
# ---------------------------------------------------------------------------
def parse_iqfeed_tick(line: str) -> TradeTick | None:
    """Parse one ``LH,...`` lookup record.

    Layout (protocol 6.2, RequestID already stripped by the client):
      LH, ts(ET), last, lastsize, totalvolume, bid, ask, tickid, basis,
      market_center, conditions, aggressor, daycode
    """
    parts = line.split(",")
    if len(parts) < 8 or parts[0] != "LH":
        return None
    try:
        ts = et_naive_to_utc(parts[1])
        price = float(parts[2])
        size = float(parts[3])
    except (ValueError, IndexError):
        return None
    if price <= 0:
        return None

    def _pos(idx: int) -> float | None:
        try:
            v = float(parts[idx])
        except (ValueError, IndexError):
            return None
        return v if v > 0 else None

    day_volume = _pos(4)
    bid = _pos(5)
    ask = _pos(6)
    try:
        sequence: int | None = int(parts[7])
    except (ValueError, IndexError):
        sequence = None
    return TradeTick(
        ts_utc=ts,
        price=price,
        size=size,
        bid=bid,
        ask=ask,
        day_volume=day_volume,
        sequence=sequence,
        frame_sha256=hashlib.sha256(line.encode("utf-8")).hexdigest(),
    )


def _et_stamp(dt_et: datetime) -> str:
    return dt_et.strftime("%Y%m%d %H%M%S")


def iter_iqfeed_window(
    client: IQFeedLookupClient,
    symbol: str,
    window_start_et: datetime,
    window_end_et: datetime,
    *,
    max_datapoints: int = IQFEED_MAX_DATAPOINTS,
    stats: dict[str, Any] | None = None,
) -> list[TradeTick]:
    """All ticks in an ET window, ascending, completeness-checked.

    ``window_end_et`` is INCLUSIVE at second resolution — that is IQFeed's own
    semantics, and callers tile windows with a one-second gap accordingly.

    A response whose record count equals ``max_datapoints`` was capped, and the
    cap drops the OLDEST records (measured; see module docstring).  The window
    is therefore CONTINUED BACKWARD until an uncapped response is seen, with the
    boundary handled by an exact ``ts <`` filter rather than by second-rounding,
    so no tick is lost or duplicated at the seam.
    """
    st = stats if stats is not None else {}
    collected: list[TradeTick] = []
    end_et = window_end_et
    oldest_seen: datetime | None = None
    for _ in range(IQFEED_MAX_CONTINUATIONS):
        try:
            res: RequestResult = client.htt(
                symbol,
                _et_stamp(window_start_et),
                _et_stamp(end_et),
                max_datapoints,
                direction=1,
                timeout_s=IQFEED_TIMEOUT_S,
            )
        except ByteCapExceeded:
            # The byte ceiling tripped, which leaves the socket desynchronized.
            # The client poisons itself for exactly this reason; reconnect and
            # retry the same window with a smaller datapoint budget.
            client.reconnect()
            st["byte_cap_retries"] = int(st.get("byte_cap_retries", 0)) + 1
            max_datapoints = max(1000, max_datapoints // 4)
            continue
        st["requests"] = int(st.get("requests", 0)) + 1
        st["bytes"] = int(st.get("bytes", 0)) + res.bytes_received
        if res.no_data:
            break
        batch: list[TradeTick] = []
        for line in res.lines:
            tick = parse_iqfeed_tick(line)
            if tick is None:
                continue
            if oldest_seen is not None and tick.ts_utc >= oldest_seen:
                continue  # already have it from the previous (newer) pass
            batch.append(tick)
        if not batch:
            break
        collected.extend(batch)
        batch_oldest = min(t.ts_utc for t in batch)
        oldest_seen = batch_oldest if oldest_seen is None else min(oldest_seen, batch_oldest)
        if res.n_records < max_datapoints:
            break  # uncapped: the window is complete
        # Capped. Continue backward from the oldest record we now hold. The
        # end bound is a whole second, so it re-includes that second; the
        # `ts < oldest_seen` filter above removes the overlap exactly.
        end_et = oldest_seen.astimezone(ET)
        if end_et <= window_start_et:
            break
        st["continuations"] = int(st.get("continuations", 0)) + 1
    else:
        raise RuntimeError(
            f"{symbol}: window {window_start_et}..{window_end_et} still truncated "
            f"after {IQFEED_MAX_CONTINUATIONS} continuations"
        )
    collected.sort(key=lambda t: (t.ts_utc, t.sequence if t.sequence is not None else 0))
    return collected


def iter_iqfeed_day(
    client: IQFeedLookupClient,
    symbol: str,
    day: date,
    *,
    window_minutes: int = IQFEED_WINDOW_MINUTES,
    stats: dict[str, Any] | None = None,
) -> Iterator[TradeTick]:
    """Every tick of one ET calendar day, ascending, one bounded window at a time."""
    cursor = datetime.combine(day, datetime.min.time(), tzinfo=ET)
    day_end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=ET)
    step = timedelta(minutes=window_minutes)
    while cursor < day_end:
        nxt = min(cursor + step, day_end)
        # Inclusive end bound: stop one second short of the next window's start.
        inclusive_end = nxt - timedelta(seconds=1)
        for tick in iter_iqfeed_window(client, symbol, cursor, inclusive_end, stats=stats):
            yield tick
        cursor = nxt


# ---------------------------------------------------------------------------
# Polygon adapter
# ---------------------------------------------------------------------------
def _polygon_ts(rec: dict[str, Any]) -> datetime | None:
    """SIP timestamp (ns since epoch, UTC) -> aware UTC datetime.

    PostgreSQL timestamps are microsecond-resolution, so the nanosecond field is
    truncated (not rounded) to microseconds.  ``timestamp_basis`` records that
    this is the SIP clock, which is what makes the loss auditable rather than
    invisible.
    """
    ns = rec.get("sip_timestamp")
    if ns is None:
        ns = rec.get("participant_timestamp")
    if ns is None:
        return None
    try:
        return datetime.fromtimestamp(int(ns) // 1000 / 1_000_000.0, tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _frame_sha(rec: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(rec, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def parse_polygon_trade(rec: dict[str, Any]) -> TradeTick | None:
    ts = _polygon_ts(rec)
    price = rec.get("price")
    if ts is None or price is None:
        return None
    try:
        price_f = float(price)
    except (TypeError, ValueError):
        return None
    if price_f <= 0:
        return None
    try:
        size = float(rec.get("size") or rec.get("decimal_size") or 0.0)
    except (TypeError, ValueError):
        size = 0.0
    seq = rec.get("sequence_number")
    return TradeTick(
        ts_utc=ts,
        price=price_f,
        size=size,
        bid=None,   # Polygon trade rows carry NO bid/ask; filled by as-of merge
        ask=None,
        day_volume=None,
        sequence=int(seq) if isinstance(seq, (int, float)) else None,
        frame_sha256=_frame_sha(rec),
    )


def parse_polygon_quote(rec: dict[str, Any]) -> QuoteTick | None:
    ts = _polygon_ts(rec)
    bid = rec.get("bid_price")
    ask = rec.get("ask_price")
    if ts is None or bid is None or ask is None:
        return None
    try:
        bid_f, ask_f = float(bid), float(ask)
    except (TypeError, ValueError):
        return None
    # Crossed/locked/zero quotes are not NBBO. The replay read already filters
    # `bid > 0 AND ask > 0 AND ask >= bid`; dropping them here keeps the row
    # counts in the batch record honest about what replay will actually see.
    if bid_f <= 0 or ask_f <= 0 or ask_f < bid_f:
        return None
    seq = rec.get("sequence_number")
    return QuoteTick(
        ts_utc=ts,
        bid=bid_f,
        ask=ask_f,
        day_volume=None,
        sequence=int(seq) if isinstance(seq, (int, float)) else None,
        frame_sha256=_frame_sha(rec),
    )


class QuoteIndex:
    """As-of index: the last NBBO at or before an instant.

    Needed because Polygon splits trades and quotes across two endpoints while
    ``iqfeed_trade_ticks`` carries bid/ask ON the trade row (IQFeed 6.2 L1 ships
    last-trade and top-of-book in one message).  Reconstructing those columns is
    therefore a MERGE, not a copy — which is exactly why Polygon-sourced trade
    rows are tagged with their own ``source`` value.

    Backed by ``array`` rather than a list of tuples: a busy day is ~10^5-10^6
    quotes, and three typed arrays cost ~12 MB where tuples would cost ~200 MB.
    """

    __slots__ = ("_ts", "_bid", "_ask")

    def __init__(self) -> None:
        self._ts = array("q")     # microseconds since epoch
        self._bid = array("d")
        self._ask = array("d")

    def add(self, quote: QuoteTick) -> None:
        self._ts.append(int(quote.ts_utc.timestamp() * 1_000_000))
        self._bid.append(quote.bid)
        self._ask.append(quote.ask)

    def __len__(self) -> int:
        return len(self._ts)

    def as_of(self, ts: datetime) -> tuple[float | None, float | None]:
        if not self._ts:
            return (None, None)
        key = int(ts.timestamp() * 1_000_000)
        i = bisect_right(self._ts, key) - 1
        if i < 0:
            return (None, None)
        return (self._bid[i], self._ask[i])


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------
def _resolve(value: Any) -> Any:
    """Evaluate a deferred batch-provenance field, or pass a plain value through."""
    return value() if callable(value) else value


def _delete_existing(cur, table: str, symbol: str, source: str,
                     start_utc: datetime, end_utc: datetime, *, naive: bool) -> int:
    """Idempotency primitive: clear this (symbol, source, day) before reloading.

    Row-level uniqueness is deliberately NOT used. A unique index over hundreds
    of millions of tape rows would cost more on every COPY than it is worth, and
    it would also REJECT genuinely duplicated provider records (two prints at the
    same microsecond with the same size are legal). Symbol-day replacement inside
    one transaction gives exact idempotency without either problem: re-running a
    symbol-day converges on precisely the rows the provider currently serves.
    """
    lo = naive_utc(start_utc) if naive else start_utc
    hi = naive_utc(end_utc) if naive else end_utc
    cur.execute(
        f"DELETE FROM {table} WHERE symbol = %s AND source = %s "
        "AND observed_at >= %s AND observed_at < %s",
        (symbol, source, lo, hi),
    )
    return cur.rowcount or 0


def _record_batch(cur, **kw: Any) -> None:
    cols = ", ".join(kw.keys())
    vals = ", ".join(["%s"] * len(kw))
    cur.execute(
        f"INSERT INTO hydration_batches ({cols}) VALUES ({vals})",
        tuple(kw.values()),
    )


# A hydratable symbol is a ticker, nothing else. hydration_jobs.symbol is varchar(32), and
# on 2026-09-05 a corpus row whose symbol field carried narrative -- "NUWE (09:30 pivot
# 5.34)" -- reached the FAILURE path, whose own _upsert_job then raised
# StringDataRightTruncation and aborted the whole 60-row pass at row 32. Two rules follow:
# a symbol that is not a ticker is REJECTED before any DB work, and recording a failure
# can never itself fail the corpus.
_HYDRATABLE_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,15}$")


def symbol_is_hydratable(symbol: str) -> bool:
    sym = str(symbol or "").strip().upper()
    return bool(sym) and sym != "UNKNOWN" and bool(_HYDRATABLE_SYMBOL_RE.match(sym))


def _upsert_job(cur, symbol: str, day: date, dataset: str, provider: str,
                status: str, rows: int, batch_id: str | None,
                error: str | None) -> None:
    cur.execute(
        """
        INSERT INTO hydration_jobs
            (symbol, trading_day, dataset, provider, status, attempts,
             rows_loaded, last_error, first_started_at, last_finished_at,
             last_batch_id)
        VALUES (%s, %s, %s, %s, %s, 1, %s, %s, now(), now(), %s)
        ON CONFLICT (symbol, trading_day, dataset, provider) DO UPDATE SET
            status = EXCLUDED.status,
            attempts = hydration_jobs.attempts + 1,
            rows_loaded = EXCLUDED.rows_loaded,
            last_error = EXCLUDED.last_error,
            last_finished_at = now(),
            last_batch_id = EXCLUDED.last_batch_id
        """,
        (symbol, day, dataset, provider, status, rows, error, batch_id),
    )


def job_status(conn, symbol: str, day: date, dataset: str, provider: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM hydration_jobs WHERE symbol=%s AND trading_day=%s "
            "AND dataset=%s AND provider=%s",
            (symbol, day, dataset, provider),
        )
        row = cur.fetchone()
    return row[0] if row else None


def load_dataset(
    conn,
    *,
    table: str,
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
    symbol: str,
    day: date,
    dataset: str,
    provider: str,
    source: str,
    basis: str,
    batch_id: str,
    requested_at: datetime,
    window: tuple[datetime, datetime],
    naive_clock: bool,
    request_count: int | Callable[[], int],
    bytes_received: int | Callable[[], int],
    provider_request: dict[str, Any] | Callable[[], dict[str, Any]],
) -> int:
    """Replace one (symbol, day, source) slice via COPY, in ONE transaction.

    ``request_count``, ``bytes_received`` and ``provider_request`` may be
    CALLABLES.  They must be, for any provider whose rows are streamed straight
    from the network into COPY: the request cost is not known until the
    generator has been drained, and snapshotting it at call time would record
    the cost of everything that happened BEFORE this batch instead of the cost
    of this batch.  Values are resolved after the COPY completes.
    """
    start_utc, end_utc = window
    stream = CopyStream(rows)
    with conn.cursor() as cur:
        deleted = _delete_existing(
            cur, table, symbol, source, start_utc, end_utc, naive=naive_clock
        )
        cur.copy_expert(
            f"COPY {table} ({', '.join(columns)}) FROM STDIN WITH (FORMAT text)",
            stream,
        )
        request_count = _resolve(request_count)
        bytes_received = _resolve(bytes_received)
        provider_request = _resolve(provider_request)
        _record_batch(
            cur,
            batch_id=batch_id,
            provider=provider,
            dataset=dataset,
            table_name=table,
            symbol=symbol,
            trading_day=day,
            source=source,
            timestamp_basis=basis,
            hydrator_version=HYDRATOR_VERSION,
            requested_at=requested_at,
            completed_at=datetime.now(UTC),
            window_start_utc=start_utc,
            window_end_utc=end_utc,
            rows_loaded=stream.rows_written,
            rows_deleted=deleted,
            request_count=request_count,
            bytes_received=bytes_received,
            payload_sha256=stream.content_sha256,
            provider_request=psycopg2.extras.Json(provider_request),
        )
        _upsert_job(
            cur, symbol, day, dataset, provider,
            "done" if stream.rows_written else "no_data",
            stream.rows_written, batch_id, None,
        )
    conn.commit()
    return stream.rows_written


# ---------------------------------------------------------------------------
# per-provider hydration
# ---------------------------------------------------------------------------
def hydrate_iqfeed_symbol_day(
    conn,
    client: IQFeedLookupClient,
    symbol: str,
    day: date,
    *,
    write_nbbo: bool = True,
    window_minutes: int = IQFEED_WINDOW_MINUTES,
) -> HydrationResult:
    """Hydrate one symbol-day from the IQFeed lookup port.

    ONE pass over the provider feeds BOTH tables, because IQFeed 6.2 carries the
    trade and the top-of-book in the same record — exactly as the live bridge
    does when it mirrors each valid-quote tick into ``momentum_nbbo_spread_tape``.
    """
    res = HydrationResult(symbol=symbol, trading_day=day, provider="iqfeed")
    t0 = time.monotonic()
    requested_at = datetime.now(UTC)
    batch_trades = str(uuid.uuid4())
    batch_nbbo = str(uuid.uuid4())
    window = et_day_bounds_utc(day)
    stats: dict[str, Any] = {}

    floor = iqfeed_retention_floor()
    if day < floor:
        res.status = "failed"
        res.error = (
            f"date {day} is older than IQFeed tick retention (floor {floor}, "
            f"{IQFEED_RETENTION_DAYS} days); no tick-fidelity source exists"
        )
        with conn.cursor() as cur:
            _upsert_job(cur, symbol, day, "trades", "iqfeed", "failed", 0, None, res.error)
        conn.commit()
        return res

    # Materialise the day once. Both tables need the same records, and a second
    # provider pass would double the request cost for identical bytes.
    ticks = list(iter_iqfeed_day(client, symbol, day, window_minutes=window_minutes, stats=stats))
    res.requests = int(stats.get("requests", 0))
    res.bytes_received = int(stats.get("bytes", 0))
    provider_request = {
        "protocol": "iqfeed_6.2",
        "command_family": "HTT",
        "window_minutes": window_minutes,
        "max_datapoints": IQFEED_MAX_DATAPOINTS,
        "continuations": int(stats.get("continuations", 0)),
        "byte_cap_retries": int(stats.get("byte_cap_retries", 0)),
        "et_day": day.isoformat(),
    }

    res.trade_rows = load_dataset(
        conn,
        table=TRADES_TABLE,
        columns=TRADE_COLUMNS,
        rows=(
            trade_copy_row(symbol, t, source=SOURCE_IQFEED_TRADES, basis=BASIS_IQFEED,
                           batch_id=batch_trades, received_at=requested_at)
            for t in ticks
        ),
        symbol=symbol, day=day, dataset="trades", provider="iqfeed",
        source=SOURCE_IQFEED_TRADES, basis=BASIS_IQFEED, batch_id=batch_trades,
        requested_at=requested_at, window=window, naive_clock=True,
        request_count=res.requests, bytes_received=res.bytes_received,
        provider_request=provider_request,
    )

    if write_nbbo:
        quotes = (
            QuoteTick(ts_utc=t.ts_utc, bid=t.bid, ask=t.ask, day_volume=t.day_volume,
                      sequence=t.sequence, frame_sha256=t.frame_sha256)
            for t in ticks
            if t.bid is not None and t.ask is not None and t.ask >= t.bid
        )
        res.nbbo_rows = load_dataset(
            conn,
            table=NBBO_TABLE,
            columns=NBBO_COLUMNS,
            rows=(
                nbbo_copy_row(symbol, q, source=SOURCE_IQFEED_NBBO, basis=BASIS_IQFEED,
                              batch_id=batch_nbbo, received_at=requested_at)
                for q in quotes
            ),
            symbol=symbol, day=day, dataset="nbbo", provider="iqfeed",
            source=SOURCE_IQFEED_NBBO, basis=BASIS_IQFEED, batch_id=batch_nbbo,
            requested_at=requested_at, window=window, naive_clock=False,
            request_count=0, bytes_received=0,
            provider_request={**provider_request, "derived_from": "at_trade_bid_ask"},
        )

    res.batch_id = batch_trades
    res.status = "done" if res.trade_rows else "no_data"
    res.wall_s = time.monotonic() - t0
    return res


def hydrate_polygon_symbol_day(
    conn,
    client: PolygonHistoricalClient,
    symbol: str,
    day: date,
    *,
    write_nbbo: bool = True,
) -> HydrationResult:
    """Hydrate one symbol-day from Polygon/Massive v3.

    Quotes are loaded FIRST and indexed, then trades stream through the as-of
    merge that fills their bid/ask.  Doing it in that order means the trade pass
    never buffers: it is a straight generator from HTTP page into COPY.
    """
    res = HydrationResult(symbol=symbol, trading_day=day, provider="polygon")
    t0 = time.monotonic()
    requested_at = datetime.now(UTC)
    start_utc, end_utc = et_day_bounds_utc(day)
    start_ns = int(start_utc.timestamp() * 1_000_000_000)
    end_ns = int(end_utc.timestamp() * 1_000_000_000)
    stats = FetchStats()
    index = QuoteIndex()
    provider_request = {
        "endpoints": ["/v3/quotes/{sym}", "/v3/trades/{sym}"],
        "timestamp_gte_ns": start_ns,
        "timestamp_lt_ns": end_ns,
        "et_day": day.isoformat(),
    }

    batch_nbbo = str(uuid.uuid4())

    def quote_rows() -> Iterator[Sequence[Any]]:
        for rec in client.iter_records("quotes", symbol, start_ns, end_ns, stats=stats):
            q = parse_polygon_quote(rec)
            if q is None:
                continue
            index.add(q)
            yield nbbo_copy_row(symbol, q, source=SOURCE_POLYGON_NBBO,
                                basis=BASIS_POLYGON, batch_id=batch_nbbo,
                                received_at=requested_at)

    # The quote index must exist before the trade merge regardless of whether the
    # NBBO rows are being persisted, so the quote pass always runs.
    if write_nbbo:
        res.nbbo_rows = load_dataset(
            conn, table=NBBO_TABLE, columns=NBBO_COLUMNS, rows=quote_rows(),
            symbol=symbol, day=day, dataset="nbbo", provider="polygon",
            source=SOURCE_POLYGON_NBBO, basis=BASIS_POLYGON, batch_id=batch_nbbo,
            requested_at=requested_at, window=(start_utc, end_utc), naive_clock=False,
            request_count=lambda: stats.requests,
            bytes_received=lambda: stats.bytes_received,
            provider_request=lambda: {**provider_request, "dataset": "quotes",
                                      "pages": stats.pages,
                                      "rate_limited": stats.rate_limited},
        )
    else:
        for rec in client.iter_records("quotes", symbol, start_ns, end_ns, stats=stats):
            q = parse_polygon_quote(rec)
            if q is not None:
                index.add(q)

    batch_trades = str(uuid.uuid4())

    def trade_rows() -> Iterator[Sequence[Any]]:
        for rec in client.iter_records("trades", symbol, start_ns, end_ns, stats=stats):
            t = parse_polygon_trade(rec)
            if t is None:
                continue
            t.bid, t.ask = index.as_of(t.ts_utc)
            yield trade_copy_row(symbol, t, source=SOURCE_POLYGON_TRADES,
                                 basis=BASIS_POLYGON, batch_id=batch_trades,
                                 received_at=requested_at)

    # Everything counted so far belongs to the QUOTE pass; subtract it so the
    # trade batch records the cost of the trade pass alone.
    before = (stats.requests, stats.bytes_received, stats.pages)
    res.trade_rows = load_dataset(
        conn, table=TRADES_TABLE, columns=TRADE_COLUMNS, rows=trade_rows(),
        symbol=symbol, day=day, dataset="trades", provider="polygon",
        source=SOURCE_POLYGON_TRADES, basis=BASIS_POLYGON, batch_id=batch_trades,
        requested_at=requested_at, window=(start_utc, end_utc), naive_clock=True,
        request_count=lambda: stats.requests - before[0],
        bytes_received=lambda: stats.bytes_received - before[1],
        provider_request=lambda: {**provider_request, "dataset": "trades",
                                  "bid_ask": "as_of_merge_from_v3_quotes",
                                  "quote_index_size": len(index),
                                  "pages": stats.pages - before[2],
                                  "rate_limited": stats.rate_limited},
    )
    res.requests = stats.requests
    res.bytes_received = stats.bytes_received
    res.batch_id = batch_trades
    res.status = "done" if res.trade_rows else "no_data"
    res.wall_s = time.monotonic() - t0
    return res


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------
def hydrate(
    pairs: Sequence[tuple[str, date]],
    *,
    provider: str = "iqfeed",
    dbname: str = DEFAULT_HYDRATED_DB,
    force: bool = False,
    write_nbbo: bool = True,
    window_minutes: int = IQFEED_WINDOW_MINUTES,
    rps: float | None = None,
    env_path: str | None = None,
    allow_market_hours: bool = False,
) -> list[HydrationResult]:
    # Both interlocks are checked BEFORE a provider socket is opened: the point
    # is to refuse, not to refuse halfway through.
    assert_outside_market_hours(allow=allow_market_hours)
    conn = connect(dbname, env_path)
    ensure_schema(conn)
    try:
        acquire_singleton_lock_or_raise(conn)
    except HydratorAlreadyRunning:
        conn.close()
        raise
    results: list[HydrationResult] = []
    iq: IQFeedLookupClient | None = None
    poly: PolygonHistoricalClient | None = None
    try:
        if provider == "iqfeed":
            iq = IQFeedLookupClient(byte_cap=IQFEED_BYTE_CAP, timeout_s=IQFEED_TIMEOUT_S)
            iq.connect()
        else:
            kwargs: dict[str, Any] = {}
            if rps:
                kwargs["rps"] = rps
            poly = PolygonHistoricalClient(**kwargs)

        for symbol, day in pairs:
            symbol = symbol.upper().strip()
            if not symbol_is_hydratable(symbol):
                r = HydrationResult(symbol[:32], day, provider, status="rejected",
                                    error="symbol_malformed")
                results.append(r)
                log.warning("[hydrator] REJECTED %r %s: not a ticker (no DB write)", symbol, day)
                log.info("[hydrator] %s", json.dumps(r.as_dict()))
                continue
            if not force and job_status(conn, symbol, day, "trades", provider) == "done":
                log.info("[hydrator] skip %s %s (already done)", symbol, day)
                results.append(HydrationResult(symbol, day, provider, status="skipped"))
                continue
            try:
                if provider == "iqfeed":
                    assert iq is not None
                    r = hydrate_iqfeed_symbol_day(
                        conn, iq, symbol, day,
                        write_nbbo=write_nbbo, window_minutes=window_minutes,
                    )
                else:
                    assert poly is not None
                    r = hydrate_polygon_symbol_day(
                        conn, poly, symbol, day, write_nbbo=write_nbbo
                    )
            except Exception as exc:  # noqa: BLE001 - one bad symbol-day must not
                conn.rollback()       # abort the corpus; it is recorded and skipped
                r = HydrationResult(symbol, day, provider, status="failed",
                                    error=f"{type(exc).__name__}: {exc}")
                try:
                    with conn.cursor() as cur:
                        _upsert_job(cur, symbol, day, "trades", provider, "failed", 0, None,
                                    (r.error or "")[:2000])
                    conn.commit()
                except Exception:  # noqa: BLE001 - recording a failure must not fail the corpus
                    conn.rollback()
                    log.warning("[hydrator] could not record the failure for %s %s", symbol, day,
                                exc_info=True)
                log.exception("[hydrator] %s %s FAILED", symbol, day)
                if provider == "iqfeed" and iq is not None:
                    try:
                        iq.reconnect()
                    except Exception:  # pragma: no cover - best effort
                        log.warning("[hydrator] iqfeed reconnect failed", exc_info=True)
            results.append(r)
            log.info("[hydrator] %s", json.dumps(r.as_dict()))
    finally:
        if iq is not None:
            iq.close()
        release_singleton_lock(conn)
        conn.close()
    return results


def parse_symbol_day(text: str) -> tuple[str, date]:
    sym, _, day = text.partition(":")
    if not sym or not day:
        raise argparse.ArgumentTypeError(f"expected SYMBOL:YYYY-MM-DD, got {text!r}")
    return sym.upper().strip(), date.fromisoformat(day.strip())


def read_pairs_csv(path: str) -> list[tuple[str, date]]:
    """Read (symbol, date) pairs from a CSV with ``symbol`` and ``date`` columns."""
    out: list[tuple[str, date]] = []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fields = {(f or "").strip().lower() for f in (reader.fieldnames or [])}
        sym_key = "symbol" if "symbol" in fields else "ticker"
        day_key = next((k for k in ("date", "trading_day", "session_date", "day") if k in fields), None)
        if day_key is None:
            raise SystemExit(f"{path}: no date column (looked for date/trading_day/session_date/day)")
        for row in reader:
            sym = (row.get(sym_key) or "").strip().upper()
            raw = (row.get(day_key) or "").strip()
            if not sym or not raw:
                continue
            out.append((sym, date.fromisoformat(raw[:10])))
    # De-duplicate while preserving order.
    seen: set[tuple[str, date]] = set()
    uniq: list[tuple[str, date]] = []
    for pair in out:
        if pair not in seen:
            seen.add(pair)
            uniq.append(pair)
    return uniq


def print_status(dbname: str, env_path: str | None = None) -> None:
    conn = connect(dbname, env_path)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT provider, dataset, status, count(*), sum(rows_loaded) "
                "FROM hydration_jobs GROUP BY 1,2,3 ORDER BY 1,2,3"
            )
            print(f"{'provider':10} {'dataset':8} {'status':10} {'jobs':>7} {'rows':>14}")
            for provider, dataset, status, jobs, rows in cur.fetchall():
                print(f"{provider:10} {dataset:8} {status:10} {jobs:>7} {rows or 0:>14}")
            cur.execute(
                "SELECT symbol, trading_day, dataset, provider, last_error "
                "FROM hydration_jobs WHERE status = 'failed' ORDER BY trading_day, symbol"
            )
            failures = cur.fetchall()
            if failures:
                print("\nFAILED:")
                for sym, day, dataset, provider, err in failures:
                    print(f"  {sym:8} {day} {dataset:7} {provider:8} {err}")
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--db-name", default=DEFAULT_HYDRATED_DB)
    ap.add_argument("--env-file", default=None,
                    help="explicit .env path (also honoured via CHILI_ENV_FILE)")
    ap.add_argument("--init-db", action="store_true",
                    help="create the hydration database + schema, then exit")
    ap.add_argument("--status", action="store_true", help="print the job ledger and exit")
    ap.add_argument("--provider", choices=("iqfeed", "polygon"), default="iqfeed")
    ap.add_argument("--symbol-day", action="append", type=parse_symbol_day, default=[],
                    metavar="SYMBOL:YYYY-MM-DD")
    ap.add_argument("--csv", help="CSV with symbol,date columns")
    ap.add_argument("--force", action="store_true", help="re-hydrate symbol-days already marked done")
    ap.add_argument("--no-nbbo", action="store_true", help="load trades only")
    ap.add_argument("--window-minutes", type=int, default=IQFEED_WINDOW_MINUTES)
    ap.add_argument("--rps", type=float, default=None, help="polygon request rate cap")
    ap.add_argument(
        "--allow-market-hours", action="store_true",
        help="permit a load inside 04:00-20:00 ET. chili_hydrated shares a "
             "postmaster, a WAL, a 4 GB buffer pool and a disk volume with the "
             "live chili; a multi-million-row COPY competes with the lane.",
    )
    ap.add_argument("--json", action="store_true", help="emit a JSON summary on stdout")
    args = ap.parse_args(argv)

    if args.env_file:
        os.environ.setdefault("CHILI_ENV_FILE", args.env_file)

    if args.init_db:
        created = ensure_database(args.db_name, args.env_file)
        conn = connect(args.db_name, args.env_file)
        try:
            ensure_schema(conn)
        finally:
            conn.close()
        print(json.dumps({"database": args.db_name, "created": created, "schema": "ready"}))
        return 0

    if args.status:
        print_status(args.db_name, args.env_file)
        return 0

    pairs = list(args.symbol_day)
    if args.csv:
        pairs.extend(read_pairs_csv(args.csv))
    if not pairs:
        ap.error("nothing to do: pass --symbol-day and/or --csv")

    t0 = time.monotonic()
    try:
        results = hydrate(
            pairs,
            provider=args.provider,
            dbname=args.db_name,
            force=args.force,
            write_nbbo=not args.no_nbbo,
            window_minutes=args.window_minutes,
            rps=args.rps,
            env_path=args.env_file,
            allow_market_hours=args.allow_market_hours,
        )
    except (HydratorAlreadyRunning, MarketHoursRefusal) as exc:
        # Exit non-zero with the sentence that explains it. A refusal that looks
        # like a crash is a refusal an operator will work around blindly.
        print(f"REFUSING: {exc}", file=sys.stderr)
        return 3
    summary = {
        "provider": args.provider,
        "symbol_days": len(results),
        "done": sum(1 for r in results if r.status == "done"),
        "no_data": sum(1 for r in results if r.status == "no_data"),
        "skipped": sum(1 for r in results if r.status == "skipped"),
        "failed": sum(1 for r in results if r.status == "failed"),
        "trade_rows": sum(r.trade_rows for r in results),
        "nbbo_rows": sum(r.nbbo_rows for r in results),
        "requests": sum(r.requests for r in results),
        "wall_s": round(time.monotonic() - t0, 2),
        "results": [r.as_dict() for r in results],
    }
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(json.dumps({k: v for k, v in summary.items() if k != "results"}, indent=2))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
