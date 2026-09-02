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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="postgresql://chili:chili@localhost:5433/chili_repro_test")
    p.add_argument("--batch", type=int, default=3600)
    p.add_argument("--batches", type=int, default=5)
    p.add_argument("--seed-rows", type=int, default=100000)
    p.add_argument("--fillfactor", type=int, default=None, help="table fillfactor (default 100)")
    a = p.parse_args(argv)
    run(a.db, a.batch, a.batches, a.seed_rows, a.fillfactor)
    return 0


if __name__ == "__main__":
    sys.exit(main())
