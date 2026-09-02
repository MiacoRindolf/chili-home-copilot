"""Benchmark the IQFeed trade-bridge tape write path on a THROWAWAY table.

Evidence tool for the regular-hours-open backlog (2026-09-02): the sole drain
loop of ``scripts/iqfeed_trade_bridge.py`` commits one bounded batch (cap
3,600 retained events) as (1) one ``INSERT ... SELECT FROM (VALUES ...)
RETURNING id`` built through SQLAlchemy ``sa.values().data()`` and then (2) a
second transaction ``UPDATE ... SET available_at WHERE id = ANY(ids)``.
Production drain-profile lines at the open show p50 4.4 s per 3,600-event batch
(insert 2.9 s, release 1.4 s), i.e. ~770 events/s ceiling.

This script reproduces the statement shapes against a table that mirrors the
``iqfeed_trade_ticks`` columns and its three indexes, inside a ``*_test``
database, and times each stage for several write strategies so the cost can be
attributed (client statement build vs server execution vs release UPDATE vs
HOT/non-HOT update behaviour).

Safety:
  * Refuses any database whose name does not end in ``_test``.
  * Creates/drops only ``bench_iqfeed_tape_write_path_test`` (+ ``_ff70``).
  * Never imports the bridge module (no socket, no live engine).

Usage (defaults are read-safe; total runtime is well under two minutes):

    python scripts/bench_iqfeed_tape_write_path.py \
        --db postgresql://chili:chili@localhost:5433/chili_repro_test \
        --batch 3600 --batches 5 --seed-rows 100000
"""
from __future__ import annotations

import argparse
import hashlib
import io
import random
import statistics
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

TABLE = "bench_iqfeed_tape_write_path_test"

COLUMNS = (
    "symbol",
    "observed_at",
    "price",
    "size",
    "bid",
    "ask",
    "provider_event_at",
    "received_at",
    "timestamp_basis",
    "bridge_version",
    "provider_trade_reference_at",
    "message_type",
    "bridge_run_id",
    "connection_generation",
    "source_frame_sequence",
    "source_frame_sha256",
)

DDL = """
CREATE TABLE {t} (
    id bigserial PRIMARY KEY,
    symbol varchar(16) NOT NULL,
    observed_at timestamp NOT NULL,
    price double precision,
    size double precision,
    bid double precision,
    ask double precision,
    source varchar(32),
    provider_event_at timestamptz,
    received_at timestamptz,
    timestamp_basis varchar(48),
    bridge_version varchar(96),
    provider_trade_reference_at timestamptz,
    message_type varchar(1),
    bridge_run_id varchar(36),
    connection_generation bigint,
    available_at timestamptz,
    source_frame_sequence bigint,
    source_frame_sha256 varchar(64)
) {with_opts};
CREATE INDEX {t}_sym_at ON {t} (symbol, observed_at DESC);
CREATE INDEX {t}_at_brin ON {t} USING brin (observed_at);
"""


def _require_test_db(dsn: str) -> None:
    name = dsn.rsplit("/", 1)[-1].split("?", 1)[0]
    if not name.endswith("_test"):
        raise SystemExit(f"refusing non-_test database: {name!r}")


def _rows(n: int, symbols: list[str], seq0: int, run_id: str, gen: int, t0: datetime) -> list[dict]:
    out = []
    for i in range(n):
        sym = random.choice(symbols)
        at = t0 + timedelta(milliseconds=i * 5)
        seq = seq0 + i
        sha = hashlib.sha256(f"{run_id}:{gen}:{seq}".encode()).hexdigest()
        px = round(random.uniform(1, 50), 4)
        out.append(
            dict(
                sym=sym,
                at=at.replace(tzinfo=None),
                px=px,
                sz=float(random.choice((100, 200, 300, 1000))),
                bid=px - 0.01,
                ask=px + 0.01,
                provider_at=at,
                received_at=at + timedelta(milliseconds=60),
                basis="provider_trade_time_ms",
                bridge="bench-v1",
                provider_trade_reference_at=at,
                message_type="T",
                bridge_run_id=run_id,
                connection_generation=gen,
                source_frame_sequence=seq,
                source_frame_sha256=sha,
            )
        )
    return out


def _bridge_values_insert(rows: list[dict]):
    """Same shape as scripts/iqfeed_trade_bridge.py::_insert_pending_batch."""
    table = sa.table(
        TABLE,
        sa.column("id", sa.BigInteger()),
        sa.column("symbol", sa.String(16)),
        sa.column("observed_at", sa.DateTime(timezone=False)),
        sa.column("price", sa.Float()),
        sa.column("size", sa.Float()),
        sa.column("bid", sa.Float()),
        sa.column("ask", sa.Float()),
        sa.column("provider_event_at", sa.DateTime(timezone=True)),
        sa.column("received_at", sa.DateTime(timezone=True)),
        sa.column("timestamp_basis", sa.String(48)),
        sa.column("bridge_version", sa.String(96)),
        sa.column("provider_trade_reference_at", sa.DateTime(timezone=True)),
        sa.column("message_type", sa.String(1)),
        sa.column("bridge_run_id", sa.String(36)),
        sa.column("connection_generation", sa.BigInteger()),
        sa.column("source_frame_sequence", sa.BigInteger()),
        sa.column("source_frame_sha256", sa.String(64)),
    )
    incoming = sa.values(
        *(sa.column(name, table.c[name].type) for name in COLUMNS),
        name="incoming_trade_rows",
    ).data(
        [
            (
                r["sym"], r["at"], r["px"], r["sz"], r["bid"], r["ask"], r["provider_at"],
                r["received_at"], r["basis"], r["bridge"], r["provider_trade_reference_at"],
                r["message_type"], r["bridge_run_id"], r["connection_generation"],
                r["source_frame_sequence"], r["source_frame_sha256"],
            )
            for r in rows
        ]
    )
    stmt = sa.insert(table).from_select(
        COLUMNS,
        sa.select(*(sa.cast(incoming.c[n], table.c[n].type).label(n) for n in COLUMNS)),
    ).returning(table.c.id)
    return stmt


def _execute_values_insert(cur, rows: list[dict]) -> list[int]:
    sql = (
        f"INSERT INTO {TABLE} ({', '.join(COLUMNS)}) VALUES %s RETURNING id"
    )
    data = [
        (
            r["sym"], r["at"], r["px"], r["sz"], r["bid"], r["ask"], r["provider_at"],
            r["received_at"], r["basis"], r["bridge"], r["provider_trade_reference_at"],
            r["message_type"], r["bridge_run_id"], r["connection_generation"],
            r["source_frame_sequence"], r["source_frame_sha256"],
        )
        for r in rows
    ]
    out = psycopg2.extras.execute_values(cur, sql, data, page_size=len(data), fetch=True)
    return [o[0] for o in out]


def _copy_insert(cur, rows: list[dict], available_at: datetime | None) -> None:
    cols = list(COLUMNS) + (["available_at"] if available_at else [])
    buf = io.StringIO()
    for r in rows:
        vals = [
            r["sym"], r["at"].isoformat(sep=" "), r["px"], r["sz"], r["bid"], r["ask"],
            r["provider_at"].isoformat(), r["received_at"].isoformat(), r["basis"], r["bridge"],
            r["provider_trade_reference_at"].isoformat(), r["message_type"], r["bridge_run_id"],
            r["connection_generation"], r["source_frame_sequence"], r["source_frame_sha256"],
        ]
        if available_at:
            vals.append(available_at.isoformat())
        buf.write("\t".join(str(v) for v in vals) + "\n")
    buf.seek(0)
    cur.copy_expert(f"COPY {TABLE} ({', '.join(cols)}) FROM STDIN", buf)


def _release(cur, ids: list[int], available_at: datetime) -> None:
    cur.execute(
        f"UPDATE {TABLE} SET available_at = %s WHERE id = ANY(%s) AND available_at IS NULL",
        (available_at, ids),
    )
    if cur.rowcount != len(ids):
        raise RuntimeError(f"release rowcount {cur.rowcount} != {len(ids)}")


def _hot_stats(cur) -> tuple[int, int]:
    cur.execute(
        "SELECT n_tup_upd, n_tup_hot_upd FROM pg_stat_user_tables WHERE relname=%s",
        (TABLE,),
    )
    row = cur.fetchone()
    return (row[0], row[1]) if row else (0, 0)


def run(dsn: str, batch: int, batches: int, seed_rows: int, fillfactor: int | None) -> None:
    _require_test_db(dsn)
    random.seed(1234)
    symbols = [f"S{i:03d}" for i in range(480)]
    run_id = str(uuid.uuid4())
    conn = psycopg2.connect(dsn, application_name="bench_iqfeed_tape_write_path")
    conn.autocommit = True
    cur = conn.cursor()
    with_opts = f"WITH (fillfactor={fillfactor})" if fillfactor else ""
    cur.execute(f"DROP TABLE IF EXISTS {TABLE}")
    cur.execute(DDL.format(t=TABLE, with_opts=with_opts))
    t0 = datetime.now(timezone.utc) - timedelta(hours=2)
    seq = 1
    ts = time.perf_counter()
    if seed_rows:
        for i in range(0, seed_rows, 20000):
            chunk = _rows(min(20000, seed_rows - i), symbols, seq, run_id, 1, t0 + timedelta(seconds=i))
            seq += len(chunk)
            _copy_insert(cur, chunk, t0)
    cur.execute(f"ANALYZE {TABLE}")
    print(f"table={TABLE} fillfactor={fillfactor or 100} seeded={seed_rows} in {time.perf_counter()-ts:.1f}s")

    conn.autocommit = False
    engine = sa.create_engine(dsn, pool_pre_ping=True)
    results: dict[str, dict[str, list[float]]] = {}

    def rec(name: str, stage: str, ms: float) -> None:
        results.setdefault(name, {}).setdefault(stage, []).append(ms)

    # --- Strategy A: bridge path (SQLAlchemy VALUES insert-from-select + RETURNING; release UPDATE) ---
    upd0, hot0 = _hot_stats(cur)
    for b in range(batches):
        rows = _rows(batch, symbols, seq, run_id, 2, datetime.now(timezone.utc))
        seq += batch
        t_a = time.perf_counter()
        stmt = _bridge_values_insert(rows)
        compiled = stmt.compile(dialect=postgresql.dialect())
        sql_len = len(str(compiled))
        t_b = time.perf_counter()
        with engine.begin() as c:
            ids = tuple(c.execute(stmt).scalars())
        t_c = time.perf_counter()
        available_at = datetime.now(timezone.utc)
        with engine.begin() as c:
            c.execute(
                sa.text(f"UPDATE {TABLE} SET available_at = :available_at WHERE id = ANY(:row_ids) AND available_at IS NULL"),
                {"available_at": available_at, "row_ids": list(ids)},
            )
        t_d = time.perf_counter()
        rec("A_bridge_sqlalchemy_values", "compile_client_ms", (t_b - t_a) * 1000)
        rec("A_bridge_sqlalchemy_values", "insert_txn_ms", (t_c - t_b) * 1000)
        rec("A_bridge_sqlalchemy_values", "release_txn_ms", (t_d - t_c) * 1000)
        rec("A_bridge_sqlalchemy_values", "sql_bytes", sql_len)
    conn.commit()
    time.sleep(0.5)
    upd1, hot1 = _hot_stats(cur)
    results["A_bridge_sqlalchemy_values"]["hot_ratio"] = [
        (hot1 - hot0) / max(1, upd1 - upd0)
    ]

    # --- Strategy B: psycopg2 execute_values + RETURNING; same release UPDATE ---
    upd0, hot0 = _hot_stats(cur)
    for b in range(batches):
        rows = _rows(batch, symbols, seq, run_id, 3, datetime.now(timezone.utc))
        seq += batch
        t_b = time.perf_counter()
        ids = _execute_values_insert(cur, rows)
        conn.commit()
        t_c = time.perf_counter()
        _release(cur, ids, datetime.now(timezone.utc))
        conn.commit()
        t_d = time.perf_counter()
        rec("B_execute_values_returning", "insert_txn_ms", (t_c - t_b) * 1000)
        rec("B_execute_values_returning", "release_txn_ms", (t_d - t_c) * 1000)
    time.sleep(0.5)
    upd1, hot1 = _hot_stats(cur)
    results["B_execute_values_returning"]["hot_ratio"] = [(hot1 - hot0) / max(1, upd1 - upd0)]

    # --- Strategy C: COPY without available_at, then release UPDATE by id range ---
    for b in range(batches):
        rows = _rows(batch, symbols, seq, run_id, 4, datetime.now(timezone.utc))
        seq += batch
        t_b = time.perf_counter()
        cur.execute(f"SELECT coalesce(max(id),0) FROM {TABLE}")
        id_lo = cur.fetchone()[0]
        _copy_insert(cur, rows, None)
        conn.commit()
        t_c = time.perf_counter()
        cur.execute(
            f"UPDATE {TABLE} SET available_at=%s WHERE id > %s AND available_at IS NULL",
            (datetime.now(timezone.utc), id_lo),
        )
        conn.commit()
        t_d = time.perf_counter()
        rec("C_copy_then_release", "insert_txn_ms", (t_c - t_b) * 1000)
        rec("C_copy_then_release", "release_txn_ms", (t_d - t_c) * 1000)

    # --- Strategy D: COPY with available_at stamped in the same row (no release UPDATE) ---
    for b in range(batches):
        rows = _rows(batch, symbols, seq, run_id, 5, datetime.now(timezone.utc))
        seq += batch
        t_b = time.perf_counter()
        _copy_insert(cur, rows, datetime.now(timezone.utc))
        conn.commit()
        t_c = time.perf_counter()
        rec("D_copy_single_txn", "insert_txn_ms", (t_c - t_b) * 1000)
        rec("D_copy_single_txn", "release_txn_ms", 0.0)

    print()
    print(f"batch={batch} batches={batches}  (median ms per batch; events/s = batch / (insert+release))")
    for name, stages in results.items():
        ins = statistics.median(stages["insert_txn_ms"])
        rel = statistics.median(stages["release_txn_ms"])
        comp = statistics.median(stages.get("compile_client_ms", [0.0]))
        tot = ins + rel
        extra = ""
        if "hot_ratio" in stages:
            extra += f" hot_update_ratio={stages['hot_ratio'][0]:.2f}"
        if "sql_bytes" in stages:
            extra += f" sql_bytes={int(statistics.median(stages['sql_bytes']))}"
        print(
            f"  {name:28s} compile={comp:7.0f} insert={ins:7.0f} release={rel:7.0f} "
            f"total={tot:7.0f}  -> {batch / (tot / 1000):6.0f} ev/s{extra}"
        )
    cur.execute(f"DROP TABLE IF EXISTS {TABLE}")
    conn.commit()
    conn.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# drain0902 suite: the SHIPPED write modes and the SHIPPED notify selection.
#
# Everything here writes only to throwaway ``*_test`` tables in a ``*_test``
# database and never imports the bridge.  ``values`` is the BEFORE number (the
# bridge's path before 2026-09-02); ``copy_prealloc`` is the AFTER number.
# ---------------------------------------------------------------------------

NBBO_TABLE = "bench_iqfeed_nbbo_test"

NBBO_COLUMNS = (
    "symbol",
    "observed_at",
    "bid",
    "ask",
    "mid",
    "spread_bps",
    "day_volume",
    "source",
    "provider_event_at",
    "received_at",
    "timestamp_basis",
    "bridge_version",
    "provider_trade_reference_at",
    "message_type",
    "bridge_run_id",
    "connection_generation",
    "source_frame_sequence",
    "source_frame_sha256",
)

NBBO_DDL = """
CREATE TABLE {t} (
    id bigserial PRIMARY KEY,
    symbol varchar(32) NOT NULL,
    observed_at timestamptz NOT NULL,
    bid double precision,
    ask double precision,
    mid double precision,
    spread_bps double precision,
    day_volume double precision,
    source varchar(24),
    provider_event_at timestamptz,
    received_at timestamptz,
    timestamp_basis varchar(48),
    bridge_version varchar(96),
    provider_trade_reference_at timestamptz,
    message_type varchar(1),
    bridge_run_id varchar(36),
    connection_generation bigint,
    available_at timestamptz,
    source_frame_sequence bigint,
    source_frame_sha256 varchar(64)
);
CREATE INDEX {t}_sym_at ON {t} (symbol, observed_at DESC);
CREATE INDEX {t}_at ON {t} (observed_at DESC);
CREATE INDEX {t}_src_at ON {t} (source, observed_at DESC);
CREATE INDEX {t}_run ON {t} (bridge_run_id, connection_generation, source_frame_sequence);
CREATE INDEX {t}_pending_release ON {t} (id)
    WHERE available_at IS NULL AND bridge_run_id IS NOT NULL;
"""

AUTHORITATIVE_BASIS = "iqfeed_q_receive_trade_reference_fenced"
OWN_CLOCK_BASIS = "iqfeed_q_bid_ask_time_clock"


def _copy_cell(value) -> str:
    """Mirror of the bridge's ``_copy_text_value`` (kept independent on purpose)."""
    if value is None:
        return "\\N"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, int):
        return str(value)
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


def _trade_tuple(r: dict) -> tuple:
    return (
        r["sym"], r["at"], r["px"], r["sz"], r["bid"], r["ask"], r["provider_at"],
        r["received_at"], r["basis"], r["bridge"], r["provider_trade_reference_at"],
        r["message_type"], r["bridge_run_id"], r["connection_generation"],
        r["source_frame_sequence"], r["source_frame_sha256"],
    )


def _nbbo_rows(n: int, symbols: list[str], seq0: int, run_id: str, gen: int, t0: datetime) -> list[dict]:
    out = []
    for i in range(n):
        sym = symbols[i % len(symbols)]
        at = t0 + timedelta(milliseconds=i * 3)
        seq = seq0 + i
        own_clock = (i % 5) == 0
        px = round(random.uniform(1, 50), 4)
        out.append(
            dict(
                sym=sym,
                # NAIVE on purpose: this is exactly what the bridge writes into
                # a timestamptz column, so it resolves against the SESSION TZ.
                at=at.replace(tzinfo=None),
                bid=px - 0.01,
                ask=px + 0.01,
                mid=px,
                spread_bps=200.0,
                provider_at=at if own_clock else None,
                received_at=at + timedelta(milliseconds=60),
                basis=OWN_CLOCK_BASIS if own_clock else AUTHORITATIVE_BASIS,
                bridge="bench-v1",
                provider_trade_reference_at=at,
                message_type="Q",
                bridge_run_id=run_id,
                connection_generation=gen,
                source_frame_sequence=seq,
                source_frame_sha256=hashlib.sha256(f"{run_id}:{gen}:{seq}".encode()).hexdigest(),
            )
        )
    return out


def _nbbo_tuple(r: dict) -> tuple:
    return (
        r["sym"], r["at"], r["bid"], r["ask"], r["mid"], r["spread_bps"], None,
        "iqfeed_l1", r["provider_at"], r["received_at"], r["basis"], r["bridge"],
        r["provider_trade_reference_at"], r["message_type"], r["bridge_run_id"],
        r["connection_generation"], r["source_frame_sequence"], r["source_frame_sha256"],
    )


def select_notify_rows(quote_rows: list[dict], trade_rows: list[dict], *, coalesce: bool) -> list[dict]:
    """Independent re-implementation of the bridge's notify selection."""
    if not coalesce:
        return list(quote_rows)
    trade_keys = {
        (r["connection_generation"], r["source_frame_sequence"]) for r in trade_rows
    }
    keep = [False] * len(quote_rows)
    newest: dict[tuple[str, str], int] = {}
    for i, row in enumerate(quote_rows):
        if (row["connection_generation"], row["source_frame_sequence"]) in trade_keys:
            keep[i] = True
            continue
        cls = "own_clock" if row["basis"] == OWN_CLOCK_BASIS else "trade_fenced"
        prev = newest.get((cls, row["sym"]))
        if prev is not None:
            keep[prev] = False
        newest[(cls, row["sym"])] = i
        keep[i] = True
    return [row for i, row in enumerate(quote_rows) if keep[i]]


def _notify_payload(row: dict, available_at: datetime) -> str:
    import json as _json

    return _json.dumps(
        {
            "symbol": row["sym"],
            "observed_at": row["provider_trade_reference_at"].isoformat(),
            "bid": row["bid"],
            "ask": row["ask"],
            "received_at": row["received_at"].isoformat(),
            "provider_event_at": row["provider_at"].isoformat() if row["provider_at"] else None,
            "provider_trade_reference_at": row["provider_trade_reference_at"].isoformat(),
            "timestamp_basis": row["basis"],
            "source": "iqfeed_l1",
            "bridge_version": row["bridge"],
            "message_type": row["message_type"],
            "bridge_run_id": row["bridge_run_id"],
            "connection_generation": row["connection_generation"],
            "source_frame_sequence": row["source_frame_sequence"],
            "source_frame_sha256": row["source_frame_sha256"],
            "available_at": available_at.isoformat(),
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _slru_notify_written(cur) -> int:
    try:
        cur.execute("SELECT blks_written FROM pg_stat_slru WHERE name = 'notify'")
        row = cur.fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def _insert_values_mode(engine, table, columns, rows, tuple_fn, col_types) -> tuple[list[int], float, float]:
    incoming = sa.values(
        *(sa.column(name, col_types[name]) for name in columns),
        name="incoming_rows",
    ).data([tuple_fn(r) for r in rows])
    stmt = sa.insert(table).from_select(
        columns,
        sa.select(*(sa.cast(incoming.c[n], col_types[n]).label(n) for n in columns)),
    ).returning(table.c.id)
    t0 = time.perf_counter()
    str(stmt.compile(dialect=postgresql.dialect()))
    t1 = time.perf_counter()
    with engine.begin() as c:
        ids = [int(v) for v in c.execute(stmt).scalars()]
    t2 = time.perf_counter()
    return ids, (t1 - t0) * 1000, (t2 - t1) * 1000


def _insert_execute_values_mode(conn, cur, table_name, columns, rows, tuple_fn) -> tuple[list[int], float, float]:
    t0 = time.perf_counter()
    data = [tuple_fn(r) for r in rows]
    t1 = time.perf_counter()
    out = psycopg2.extras.execute_values(
        cur,
        f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES %s RETURNING id",
        data,
        page_size=len(data),
        fetch=True,
    )
    conn.commit()
    t2 = time.perf_counter()
    return [int(o[0]) for o in out], (t1 - t0) * 1000, (t2 - t1) * 1000


def _insert_copy_prealloc_mode(conn, cur, table_name, columns, rows, tuple_fn) -> tuple[list[int], float, float]:
    t0 = time.perf_counter()
    cur.execute(
        "SELECT nextval(pg_get_serial_sequence(%s, 'id')) FROM generate_series(1, %s)",
        (table_name, len(rows)),
    )
    ids = [int(r[0]) for r in cur.fetchall()]
    buf = io.StringIO()
    for row_id, r in zip(ids, rows):
        buf.write("\t".join(_copy_cell(v) for v in (row_id, *tuple_fn(r))) + "\n")
    buf.seek(0)
    t1 = time.perf_counter()
    cur.copy_expert(
        f"COPY {table_name} (id, {', '.join(columns)}) FROM STDIN WITH (FORMAT text)",
        buf,
    )
    if cur.rowcount not in (-1, len(rows)):
        raise RuntimeError(f"COPY rowcount {cur.rowcount} != {len(rows)}")
    conn.commit()
    t2 = time.perf_counter()
    return ids, (t1 - t0) * 1000, (t2 - t1) * 1000


def run_drain0902(
    dsn: str,
    *,
    modes: list[str],
    notify_modes: list[str],
    batch_events: int,
    batches: int,
    seed_rows: int,
    json_out: str | None,
) -> dict:
    _require_test_db(dsn)
    random.seed(4242)
    symbols = [f"S{i:03d}" for i in range(200)]  # realistic open mix: 100-300
    run_id = str(uuid.uuid4())
    conn = psycopg2.connect(dsn, application_name="bench_iqfeed_drain0902")
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SET TIME ZONE 'UTC'")
    for table, ddl in ((TABLE, DDL), (NBBO_TABLE, NBBO_DDL)):
        cur.execute(f"DROP TABLE IF EXISTS {table}")
        cur.execute(ddl.format(t=table, with_opts=""))
    t0 = datetime.now(timezone.utc) - timedelta(hours=2)
    seq = 1
    if seed_rows:
        for i in range(0, seed_rows, 20000):
            chunk = _rows(min(20000, seed_rows - i), symbols, seq, run_id, 1, t0 + timedelta(seconds=i))
            seq += len(chunk)
            _copy_insert(cur, chunk, t0)
        for i in range(0, seed_rows, 20000):
            chunk = _nbbo_rows(min(20000, seed_rows - i), symbols, seq, run_id, 1, t0 + timedelta(seconds=i))
            seq += len(chunk)
            buf = io.StringIO()
            for r in chunk:
                buf.write("\t".join(_copy_cell(v) for v in _nbbo_tuple(r)) + "\n")
            buf.seek(0)
            cur.copy_expert(f"COPY {NBBO_TABLE} ({', '.join(NBBO_COLUMNS)}) FROM STDIN", buf)
    cur.execute(f"ANALYZE {TABLE}")
    cur.execute(f"ANALYZE {NBBO_TABLE}")
    print(f"seeded {seed_rows} rows into {TABLE} and {NBBO_TABLE}")

    conn.autocommit = False
    engine = sa.create_engine(dsn, pool_pre_ping=True)
    sa.event.listens_for(engine, "connect")(
        lambda dbapi, rec: dbapi.cursor().execute("SET TIME ZONE 'UTC'")
    )
    trade_table = sa.table(TABLE, sa.column("id", sa.BigInteger()), *(
        sa.column(n, t) for n, t in _TRADE_COL_TYPES.items()
    ))
    nbbo_table = sa.table(NBBO_TABLE, sa.column("id", sa.BigInteger()), *(
        sa.column(n, t) for n, t in _NBBO_COL_TYPES.items()
    ))

    results: dict[str, dict] = {}
    half = batch_events // 2
    for mode in modes:
        build, execute, commit_rel, evs = [], [], [], []
        for _ in range(batches):
            trades = _rows(half, symbols, seq, run_id, 9, datetime.now(timezone.utc))
            seq += half
            quotes = _nbbo_rows(half, symbols, seq, run_id, 9, datetime.now(timezone.utc))
            seq += half
            b = e = 0.0
            all_ids: list[tuple[str, list[int]]] = []
            for table_name, columns, rows, tuple_fn, sa_table, col_types in (
                (TABLE, COLUMNS, trades, _trade_tuple, trade_table, _TRADE_COL_TYPES),
                (NBBO_TABLE, NBBO_COLUMNS, quotes, _nbbo_tuple, nbbo_table, _NBBO_COL_TYPES),
            ):
                if mode == "values":
                    ids, bb, ee = _insert_values_mode(engine, sa_table, columns, rows, tuple_fn, col_types)
                elif mode == "execute_values":
                    ids, bb, ee = _insert_execute_values_mode(conn, cur, table_name, columns, rows, tuple_fn)
                elif mode == "copy_prealloc":
                    ids, bb, ee = _insert_copy_prealloc_mode(conn, cur, table_name, columns, rows, tuple_fn)
                else:
                    raise SystemExit(f"unknown mode {mode!r}")
                b += bb
                e += ee
                all_ids.append((table_name, ids))
            t_rel = time.perf_counter()
            for table_name, ids in all_ids:
                cur.execute(
                    f"UPDATE {table_name} SET available_at=%s WHERE id = ANY(%s) AND available_at IS NULL",
                    (datetime.now(timezone.utc), ids),
                )
                if cur.rowcount != len(ids):
                    raise RuntimeError(f"{table_name} release rowcount {cur.rowcount} != {len(ids)}")
            conn.commit()
            rel_ms = (time.perf_counter() - t_rel) * 1000
            build.append(b)
            execute.append(e)
            commit_rel.append(rel_ms)
            evs.append(batch_events / ((b + e + rel_ms) / 1000))
        results[f"mode:{mode}"] = {
            "client_build_ms": round(statistics.median(build), 1),
            "execute_ms": round(statistics.median(execute), 1),
            "release_txn_ms": round(statistics.median(commit_rel), 1),
            "events_per_s": round(statistics.median(evs)),
            "batch_events": batch_events,
        }

    # NOTIFY cost, isolated: the payload bytes committed per release batch.
    quotes = _nbbo_rows(half, symbols, seq, run_id, 11, datetime.now(timezone.utc))
    seq += half
    trades = _rows(half // 2, symbols, seq, run_id, 11, datetime.now(timezone.utc))
    seq += half // 2
    available_at = datetime.now(timezone.utc)
    for notify_mode in notify_modes:
        if notify_mode == "none":
            selected: list[dict] = []
        elif notify_mode == "coalesced":
            selected = select_notify_rows(quotes, trades, coalesce=True)
        else:
            selected = select_notify_rows(quotes, trades, coalesce=False)
        payloads = [_notify_payload(r, available_at) for r in selected]
        slru0 = _slru_notify_written(cur)
        conn.commit()
        t0n = time.perf_counter()
        if payloads:
            psycopg2.extras.execute_values(
                cur,
                "SELECT pg_notify('bench_iqfeed_l1', v.payload) FROM (VALUES %s) AS v(payload)",
                [(p,) for p in payloads],
                page_size=len(payloads),
            )
            cur.fetchall()
        t1n = time.perf_counter()
        conn.commit()
        t2n = time.perf_counter()
        slru1 = _slru_notify_written(cur)
        conn.commit()
        results[f"notify:{notify_mode}"] = {
            "payload_count": len(payloads),
            "distinct_symbols": len({r["sym"] for r in selected}),
            "payload_bytes": sum(len(p) for p in payloads),
            "execute_ms": round((t1n - t0n) * 1000, 1),
            "commit_ms": round((t2n - t1n) * 1000, 1),
            "slru_notify_blks_written": max(0, slru1 - slru0),
        }

    conn.commit()
    cur.execute(f"DROP TABLE IF EXISTS {TABLE}")
    cur.execute(f"DROP TABLE IF EXISTS {NBBO_TABLE}")
    conn.commit()
    conn.close()
    engine.dispose()
    print()
    for name, row in results.items():
        print(f"  {name:26s} " + "  ".join(f"{k}={v}" for k, v in row.items()))
    if json_out:
        import json as _json

        pathlib_write = open(json_out, "w", encoding="utf-8")
        try:
            _json.dump(results, pathlib_write, indent=2, sort_keys=True)
        finally:
            pathlib_write.close()
        print(f"\nwrote {json_out}")
    return results


_TRADE_COL_TYPES = {
    "symbol": sa.String(16),
    "observed_at": sa.DateTime(timezone=False),
    "price": sa.Float(),
    "size": sa.Float(),
    "bid": sa.Float(),
    "ask": sa.Float(),
    "provider_event_at": sa.DateTime(timezone=True),
    "received_at": sa.DateTime(timezone=True),
    "timestamp_basis": sa.String(48),
    "bridge_version": sa.String(96),
    "provider_trade_reference_at": sa.DateTime(timezone=True),
    "message_type": sa.String(1),
    "bridge_run_id": sa.String(36),
    "connection_generation": sa.BigInteger(),
    "source_frame_sequence": sa.BigInteger(),
    "source_frame_sha256": sa.String(64),
}
_NBBO_COL_TYPES = {
    "symbol": sa.String(32),
    "observed_at": sa.DateTime(timezone=True),
    "bid": sa.Float(),
    "ask": sa.Float(),
    "mid": sa.Float(),
    "spread_bps": sa.Float(),
    "day_volume": sa.Float(),
    "source": sa.String(24),
    "provider_event_at": sa.DateTime(timezone=True),
    "received_at": sa.DateTime(timezone=True),
    "timestamp_basis": sa.String(48),
    "bridge_version": sa.String(96),
    "provider_trade_reference_at": sa.DateTime(timezone=True),
    "message_type": sa.String(1),
    "bridge_run_id": sa.String(36),
    "connection_generation": sa.BigInteger(),
    "source_frame_sequence": sa.BigInteger(),
    "source_frame_sha256": sa.String(64),
}


def verify_parity(dsn: str, *, rows_n: int = 500, session_timezones=("UTC", "America/Los_Angeles")) -> bool:
    """Prove values / execute_values / copy land IDENTICAL bytes, under BOTH TZs.

    The bridge pins ``SET TIME ZONE 'UTC'`` on every connection; this runs the
    same pin under a non-UTC server default so the NAIVE nbbo ``at`` and the
    aware trade clocks are proven to land the same way in all three paths.
    """
    _require_test_db(dsn)
    random.seed(99)
    symbols = [f"P{i:03d}" for i in range(40)]
    run_id = str(uuid.uuid4())
    ok = True
    for session_tz in session_timezones:
        conn = psycopg2.connect(dsn, application_name="bench_iqfeed_parity")
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(f"SET TIME ZONE '{session_tz}'")
        # ... then the bridge's own pin, exactly as the connect event does.
        cur.execute("SET TIME ZONE 'UTC'")
        tables = {}
        for mode in ("values", "execute_values", "copy_prealloc"):
            for base, ddl in ((TABLE, DDL), (NBBO_TABLE, NBBO_DDL)):
                name = f"{base}_{mode}"
                cur.execute(f"DROP TABLE IF EXISTS {name}")
                cur.execute(ddl.format(t=name, with_opts=""))
                tables[(mode, base)] = name
        t0 = datetime.now(timezone.utc)
        trades = _rows(rows_n, symbols, 1, run_id, 1, t0)
        quotes = _nbbo_rows(rows_n, symbols, 10_000, run_id, 1, t0)
        engine = sa.create_engine(dsn, pool_pre_ping=True)
        sa.event.listens_for(engine, "connect")(
            lambda dbapi, rec: dbapi.cursor().execute("SET TIME ZONE 'UTC'")
        )
        conn.autocommit = False
        for base, columns, rows, tuple_fn, col_types in (
            (TABLE, COLUMNS, trades, _trade_tuple, _TRADE_COL_TYPES),
            (NBBO_TABLE, NBBO_COLUMNS, quotes, _nbbo_tuple, _NBBO_COL_TYPES),
        ):
            for mode in ("values", "execute_values", "copy_prealloc"):
                name = tables[(mode, base)]
                if mode == "values":
                    tbl = sa.table(name, sa.column("id", sa.BigInteger()), *(
                        sa.column(n, t) for n, t in col_types.items()
                    ))
                    _insert_values_mode(engine, tbl, columns, rows, tuple_fn, col_types)
                elif mode == "execute_values":
                    _insert_execute_values_mode(conn, cur, name, columns, rows, tuple_fn)
                else:
                    _insert_copy_prealloc_mode(conn, cur, name, columns, rows, tuple_fn)
            reference = tables[("values", base)]
            for mode in ("execute_values", "copy_prealloc"):
                other = tables[(mode, base)]
                cols = ", ".join(columns)
                for a_name, b_name in ((reference, other), (other, reference)):
                    cur.execute(
                        f"SELECT count(*) FROM ("
                        f"SELECT {cols} FROM {a_name} EXCEPT ALL "
                        f"SELECT {cols} FROM {b_name}) d"
                    )
                    diff = cur.fetchone()[0]
                    if diff:
                        ok = False
                        print(f"  PARITY FAIL tz={session_tz} {base} {mode}: {diff} rows differ")
            conn.commit()
        for name in tables.values():
            cur.execute(f"DROP TABLE IF EXISTS {name}")
        conn.commit()
        conn.close()
        engine.dispose()
        print(f"  parity tz={session_tz}: {'OK' if ok else 'FAILED'}")
    return ok


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="postgresql://chili:chili@localhost:5433/chili_repro_test")
    p.add_argument("--batch", type=int, default=3600)
    p.add_argument("--batches", type=int, default=5)
    p.add_argument("--seed-rows", type=int, default=100000)
    p.add_argument("--fillfactor", type=int, default=None, help="table fillfactor (default 100)")
    p.add_argument("--suite", choices=("strategies", "drain0902"), default="strategies")
    p.add_argument("--mode", default="values,execute_values,copy_prealloc")
    p.add_argument("--notify", default="all,coalesced,none")
    p.add_argument("--batch-events", type=int, default=3600)
    p.add_argument("--verify-parity", action="store_true")
    p.add_argument("--json", dest="json_out", default=None)
    a = p.parse_args(argv)
    if a.verify_parity:
        return 0 if verify_parity(a.db) else 1
    if a.suite == "drain0902":
        run_drain0902(
            a.db,
            modes=[m for m in a.mode.split(",") if m],
            notify_modes=[m for m in a.notify.split(",") if m],
            batch_events=a.batch_events,
            batches=a.batches,
            seed_rows=a.seed_rows,
            json_out=a.json_out,
        )
        return 0
    run(a.db, a.batch, a.batches, a.seed_rows, a.fillfactor)
    return 0


if __name__ == "__main__":
    sys.exit(main())
