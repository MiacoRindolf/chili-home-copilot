"""Bench: saan napupunta ang 4-6 s kada 3,600-event drain ng L1 trade bridge.

Sinusukat ang EKSAKTONG insert/release na landas ng ``scripts/iqfeed_trade_bridge.py``
(``_insert_pending_batch`` + ``_release_pending_batch``, hindi kopya) laban sa
mga THROWAWAY na bench table na ito mismo ang lumilikha sa isang ``*_test`` na DB,
tapos inihahambing sa dalawang alternatibong insert na nagpapanatili ng parehong
dalawang-transaksyon na kontrata (insert → hiwalay na release by primary key):

    current   SQLAlchemy VALUES(...) → INSERT ... SELECT ... RETURNING id
    exec_vals psycopg2.extras.execute_values (multi-row VALUES, RETURNING id)
    copy      nextval() pre-allocation ng id + COPY ... FROM STDIN (text)

Kada stage ay hinahati sa CLIENT (SQLAlchemy compile + psycopg2 mogrify) at
CURSOR (server + wire) gamit ang before/after_cursor_execute events, kaya
lumalabas kung Python CPU ba o Postgres ang dominante.

HARD GUARDS
  * ang DB name ay DAPAT magtapos sa ``_test`` (HINDI kailanman ang live ``chili``)
  * tanging ``*_bench_test`` na table ang sinusulatan; nililikha at pinapatay
    ng script na ito mismo
  * walang binabasa o sinusulatan sa ``iqfeed_trade_ticks`` /
    ``momentum_nbbo_spread_tape`` ng anumang DB

Runnable (post-RTH, hindi kasabay ng ibang pytest):
    BENCH_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_b_test \
      python scripts/bench_iqfeed_tape_drain.py --rows 1800 --quotes 1800 --repeat 3
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import statistics
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import psycopg2.extras
import sqlalchemy as sa
from sqlalchemy import event

_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.iqfeed_drain_capacity import (  # noqa: E402
    DrainCostModel,
    fit_per_event_cost,
)

TRADE_BENCH = "iqfeed_trade_ticks_bench_test"
NBBO_BENCH = "momentum_nbbo_spread_tape_bench_test"
TRADE_SEQ = "iqfeed_trade_ticks_bench_test_id_seq"
NBBO_SEQ = "momentum_nbbo_spread_tape_bench_test_id_seq"

# EKSAKTONG production index set (pg_indexes ng live chili, 2026-09-02), kasama
# ang BRIN at ang partial "pending release" index na WALA sa migrations ng test DB.
TRADE_DDL = """
CREATE TABLE {t} (
    id bigserial PRIMARY KEY,
    symbol varchar(16), observed_at timestamp without time zone,
    price double precision, size double precision, bid double precision, ask double precision,
    source varchar(24), provider_event_at timestamptz, received_at timestamptz,
    timestamp_basis varchar(48), bridge_version varchar(96), provider_trade_reference_at timestamptz,
    message_type varchar(1), bridge_run_id varchar(36), connection_generation bigint,
    available_at timestamptz, source_frame_sequence bigint, source_frame_sha256 varchar(64)
) {with_};
CREATE INDEX {t}_sym_at ON {t} USING btree (symbol, observed_at DESC);
CREATE INDEX {t}_at_brin ON {t} USING brin (observed_at);
"""
NBBO_DDL = """
CREATE TABLE {t} (
    id bigserial PRIMARY KEY,
    symbol varchar(32), observed_at timestamptz,
    bid double precision, ask double precision, mid double precision, spread_bps double precision,
    day_volume double precision, source varchar(24), provider_event_at timestamptz, received_at timestamptz,
    timestamp_basis varchar(48), bridge_version varchar(96), provider_trade_reference_at timestamptz,
    message_type varchar(1), bridge_run_id varchar(36), connection_generation bigint,
    available_at timestamptz, source_frame_sequence bigint, source_frame_sha256 varchar(64)
) {with_};
CREATE INDEX {t}_symbol_observed ON {t} USING btree (symbol, observed_at DESC);
CREATE INDEX {t}_observed ON {t} USING btree (observed_at);
CREATE INDEX {t}_source_symbol_observed ON {t} USING btree (source, symbol, observed_at DESC);
CREATE INDEX {t}_pending_release ON {t} USING btree (bridge_run_id, connection_generation, source_frame_sequence)
    WHERE available_at IS NULL AND bridge_run_id IS NOT NULL;
"""

TRADE_COLS = (
    "symbol", "observed_at", "price", "size", "bid", "ask", "provider_event_at",
    "received_at", "timestamp_basis", "bridge_version", "provider_trade_reference_at",
    "message_type", "bridge_run_id", "connection_generation", "source_frame_sequence",
    "source_frame_sha256",
)
NBBO_COLS = (
    "symbol", "observed_at", "bid", "ask", "mid", "spread_bps", "day_volume", "source",
    "provider_event_at", "received_at", "timestamp_basis", "bridge_version",
    "provider_trade_reference_at", "message_type", "bridge_run_id", "connection_generation",
    "source_frame_sequence", "source_frame_sha256",
)


def _load_bridge():
    path = _HERE / "iqfeed_trade_bridge.py"
    spec = importlib.util.spec_from_file_location("iqfeed_trade_bridge_bench", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["iqfeed_trade_bridge_bench"] = module
    spec.loader.exec_module(module)
    return module


def _bench_table(name: str, template: sa.TableClause) -> sa.TableClause:
    return sa.table(name, *(sa.column(c.name, c.type) for c in template.columns))


def _rows(bridge, n_trades: int, n_quotes: int, *, symbols: int = 480):
    """Synthetic rows in the exact dict shape the writer hands to the DB path."""
    now = datetime.now(timezone.utc)
    run_id = str(uuid.uuid4())
    trades, quotes = [], []
    seq = 0
    frames = max(n_trades, n_quotes)
    for i in range(frames):
        seq += 1
        sym = f"S{i % symbols:04d}"
        at = now - timedelta(seconds=(frames - i) * 0.001)
        sha = hashlib.sha256(f"{run_id}:{seq}".encode()).hexdigest()
        base = {
            "sym": sym, "received_at": at, "basis": bridge.EXACT_PRINT_TIMESTAMP_BASIS,
            "bridge": bridge.BRIDGE_BUILD, "message_type": "Q", "bridge_run_id": run_id,
            "connection_generation": 1, "source_frame_sequence": seq,
            "source_frame_sha256": sha, "provider_trade_reference_at": at,
        }
        if len(trades) < n_trades:
            trades.append({**base, "at": at.replace(tzinfo=None), "px": 10.0 + i % 7,
                           "sz": 100.0, "bid": 9.99, "ask": 10.01, "provider_at": at})
        if len(quotes) < n_quotes:
            quotes.append({**base, "at": at.replace(tzinfo=None), "bid": 9.99, "ask": 10.01,
                           "mid": 10.0, "spread_bps": 20.0, "provider_at": None,
                           "basis": bridge.AUTHORITATIVE_TIMESTAMP_BASIS})
    return trades, quotes


_ACTIVE_TIMER: "CursorTimer | None" = None


class _TimedConnection(psycopg2.extensions.connection):
    """Ang COMMIT ay DBAPI call, hindi cursor event — dito nakatago ang gastos ng
    1,800 pending NOTIFY kada release (pg_notify SLRU page writes sa commit)."""

    def commit(self):
        t0 = time.perf_counter()
        try:
            return super().commit()
        finally:
            if _ACTIVE_TIMER is not None:
                _ACTIVE_TIMER.commit_s += time.perf_counter() - t0


class CursorTimer:
    """Hinahati ang bawat execute sa cursor (server+wire), commit, at client (compile+mogrify)."""

    def __init__(self, engine):
        self.cursor_s = 0.0
        self.mogrify_s = 0.0
        self.commit_s = 0.0
        self.sql_bytes = 0
        self.statements = 0
        event.listen(engine, "before_cursor_execute", self._before)
        event.listen(engine, "after_cursor_execute", self._after)

    def reset(self):
        self.cursor_s = self.mogrify_s = self.commit_s = 0.0
        self.sql_bytes = self.statements = 0

    def _before(self, conn, cursor, statement, parameters, context, executemany):
        t0 = time.perf_counter()
        try:
            rendered = cursor.mogrify(statement, parameters)
            self.sql_bytes += len(rendered)
        except Exception:
            self.sql_bytes += len(statement)
        self.mogrify_s += time.perf_counter() - t0
        context._bench_t0 = time.perf_counter()

    def _after(self, conn, cursor, statement, parameters, context, executemany):
        self.cursor_s += time.perf_counter() - getattr(context, "_bench_t0", time.perf_counter())
        self.statements += 1


def _insert_current(bridge, engine, trades, quotes):
    with engine.begin() as c:
        return bridge._insert_pending_batch(c, trade_rows=trades, quote_rows=quotes, return_row_ids=True)


def _insert_execute_values(bridge, engine, trades, quotes):
    def _vals(rows, cols, mapping):
        return [tuple(mapping(r, c) for c in cols) for r in rows]

    def _t(r, c):
        return r.get({"symbol": "sym", "observed_at": "at", "price": "px", "size": "sz",
                      "provider_event_at": "provider_at", "timestamp_basis": "basis",
                      "bridge_version": "bridge"}.get(c, c))

    def _q(r, c):
        if c == "day_volume":
            return None
        if c == "source":
            return "iqfeed_l1"
        return r.get({"symbol": "sym", "observed_at": "at", "provider_event_at": "provider_at",
                      "timestamp_basis": "basis", "bridge_version": "bridge"}.get(c, c))

    with engine.begin() as c:
        raw = c.connection.cursor()
        tid = psycopg2.extras.execute_values(
            raw, f"INSERT INTO {TRADE_BENCH} ({', '.join(TRADE_COLS)}) VALUES %s RETURNING id",
            _vals(trades, TRADE_COLS, _t), page_size=max(1, len(trades)), fetch=True,
        ) if trades else []
        qid = psycopg2.extras.execute_values(
            raw, f"INSERT INTO {NBBO_BENCH} ({', '.join(NBBO_COLS)}) VALUES %s RETURNING id",
            _vals(quotes, NBBO_COLS, _q), page_size=max(1, len(quotes)), fetch=True,
        ) if quotes else []
        return tuple(r[0] for r in tid), tuple(r[0] for r in qid)


def _copy_text(v) -> str:
    if v is None:
        return "\\N"
    if isinstance(v, datetime):
        return v.isoformat()
    s = str(v)
    return s.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n")


def _insert_copy(bridge, engine, trades, quotes):
    """id pre-allocation (isang nextval batch) + COPY text; same release-by-id contract."""
    with engine.begin() as c:
        raw = c.connection.cursor()
        tid = qid = ()
        if trades:
            raw.execute(f"SELECT nextval('{TRADE_SEQ}') FROM generate_series(1, %s)", (len(trades),))
            tid = tuple(r[0] for r in raw.fetchall())
            buf = io.StringIO()
            for rid, r in zip(tid, trades):
                buf.write("\t".join([str(rid)] + [_copy_text(v) for v in (
                    r["sym"], r["at"], r["px"], r["sz"], r["bid"], r["ask"], r["provider_at"],
                    r["received_at"], r["basis"], r["bridge"], r["provider_trade_reference_at"],
                    r["message_type"], r["bridge_run_id"], r["connection_generation"],
                    r["source_frame_sequence"], r["source_frame_sha256"])]) + "\n")
            buf.seek(0)
            raw.copy_expert(f"COPY {TRADE_BENCH} (id, {', '.join(TRADE_COLS)}) FROM STDIN", buf)
        if quotes:
            raw.execute(f"SELECT nextval('{NBBO_SEQ}') FROM generate_series(1, %s)", (len(quotes),))
            qid = tuple(r[0] for r in raw.fetchall())
            buf = io.StringIO()
            for rid, r in zip(qid, quotes):
                buf.write("\t".join([str(rid)] + [_copy_text(v) for v in (
                    r["sym"], r["at"], r["bid"], r["ask"], r["mid"], r["spread_bps"], None,
                    "iqfeed_l1", r["provider_at"], r["received_at"], r["basis"], r["bridge"],
                    r["provider_trade_reference_at"], r["message_type"], r["bridge_run_id"],
                    r["connection_generation"], r["source_frame_sequence"],
                    r["source_frame_sha256"])]) + "\n")
            buf.seek(0)
            raw.copy_expert(f"COPY {NBBO_BENCH} (id, {', '.join(NBBO_COLS)}) FROM STDIN", buf)
        return tid, qid


def _release_current(bridge, engine, trades, quotes, tid, qid, *, notify: bool):
    prev = bridge.IQFEED_NOTIFY_ENABLED
    bridge.IQFEED_NOTIFY_ENABLED = notify
    try:
        with engine.begin() as c:
            bridge._release_pending_batch(
                c, trade_rows=trades, quote_rows=quotes,
                available_at=datetime.now(timezone.utc), trade_row_ids=tid, quote_row_ids=qid,
            )
    finally:
        bridge.IQFEED_NOTIFY_ENABLED = prev


def _hot_stats(engine):
    with engine.connect() as c:
        return {
            r[0]: (r[1], r[2]) for r in c.execute(sa.text(
                "select relname, n_tup_upd, n_tup_hot_upd from pg_stat_user_tables "
                "where relname in (:a, :b)"), {"a": TRADE_BENCH, "b": NBBO_BENCH})
        }


def _setup(engine, *, fillfactor: int | None):
    with_ = f"WITH (fillfactor={fillfactor})" if fillfactor else ""
    with engine.begin() as c:
        c.execute(sa.text(f"DROP TABLE IF EXISTS {TRADE_BENCH}"))
        c.execute(sa.text(f"DROP TABLE IF EXISTS {NBBO_BENCH}"))
        for stmt in TRADE_DDL.format(t=TRADE_BENCH, with_=with_).split(";"):
            if stmt.strip():
                c.execute(sa.text(stmt))
        for stmt in NBBO_DDL.format(t=NBBO_BENCH, with_=with_).split(";"):
            if stmt.strip():
                c.execute(sa.text(stmt))


def _teardown(engine):
    with engine.begin() as c:
        c.execute(sa.text(f"DROP TABLE IF EXISTS {TRADE_BENCH}"))
        c.execute(sa.text(f"DROP TABLE IF EXISTS {NBBO_BENCH}"))


def run(args) -> dict:
    url = os.environ.get("BENCH_DATABASE_URL") or args.database_url
    if not url:
        raise SystemExit("BENCH_DATABASE_URL (a *_test database) is required")
    dbname = sa.engine.make_url(url).database or ""
    if not dbname.endswith("_test"):
        raise SystemExit(f"refusing: database {dbname!r} does not end in _test")

    global _ACTIVE_TIMER
    bridge = _load_bridge()
    engine = sa.create_engine(
        url, pool_pre_ping=True, connect_args={"connection_factory": _TimedConnection},
    )
    timer = CursorTimer(engine)
    _ACTIVE_TIMER = timer
    # Point the REAL bridge DB path at the throwaway tables.
    bridge._TRADE_WRITE_TABLE = _bench_table(TRADE_BENCH, bridge._TRADE_WRITE_TABLE)
    bridge._NBBO_WRITE_TABLE = _bench_table(NBBO_BENCH, bridge._NBBO_WRITE_TABLE)
    bridge.MARK_TRADE_IDS_AVAILABLE = sa.text(
        f"UPDATE {TRADE_BENCH} SET available_at = :available_at WHERE id = ANY(:row_ids) AND available_at IS NULL")
    bridge.MARK_NBBO_IDS_AVAILABLE = sa.text(
        f"UPDATE {NBBO_BENCH} SET available_at = :available_at WHERE id = ANY(:row_ids) AND available_at IS NULL")

    inserts = {"current": _insert_current, "exec_vals": _insert_execute_values, "copy": _insert_copy}
    report: dict = {"rows": args.rows, "quotes": args.quotes, "repeat": args.repeat, "db": dbname,
                    "variants": {}}

    def _measure(label, fn):
        samples = []
        for i in range(args.repeat + 1):
            timer.reset()
            t0 = time.perf_counter()
            out = fn()
            wall = time.perf_counter() - t0
            if i == 0:
                continue  # warm-up
            samples.append({"wall_ms": wall * 1e3, "cursor_ms": timer.cursor_s * 1e3,
                            "commit_ms": timer.commit_s * 1e3,
                            "mogrify_ms": timer.mogrify_s * 1e3,
                            "client_ms": (wall - timer.cursor_s - timer.commit_s) * 1e3,
                            "sql_bytes": timer.sql_bytes, "statements": timer.statements})
        med = {k: statistics.median(s[k] for s in samples) for k in samples[0]}
        report["variants"][label] = med
        print(f"  {label:<40} wall={med['wall_ms']:6.0f}ms  cursor={med['cursor_ms']:5.0f}ms  "
              f"commit={med['commit_ms']:5.0f}ms  client={med['client_ms']:5.0f}ms "
              f"(mogrify {med['mogrify_ms']:4.0f}ms)  sql={med['sql_bytes']/1e6:5.2f}MB")
        return out

    for fillfactor in (None, 90):
        tag = "ff100(prod)" if fillfactor is None else f"ff{fillfactor}"
        print(f"\n== tables {tag}: {args.rows} trades + {args.quotes} quotes per batch")
        _setup(engine, fillfactor=fillfactor)
        for name, fn in inserts.items():
            trades, quotes = _rows(bridge, args.rows, args.quotes)
            holder = {}

            def _ins(fn=fn, holder=holder, trades=trades, quotes=quotes):
                holder["ids"] = fn(bridge, engine, trades, quotes)
                return holder["ids"]

            _measure(f"[{tag}] insert/{name}", _ins)
        # Release attribution on FRESH rows each time (an already-released row is a no-op).
        for notify in (True, False):
            def _ins_then_rel(notify=notify):
                trades, quotes = _rows(bridge, args.rows, args.quotes)
                tid, qid = _insert_copy(bridge, engine, trades, quotes)
                timer.reset()
                t0 = time.perf_counter()
                _release_current(bridge, engine, trades, quotes, tid, qid, notify=notify)
                return time.perf_counter() - t0
            label = f"[{tag}] release/update+{'notify' if notify else 'no-notify'}"
            _measure(label, _ins_then_rel)
        # Trade-only vs quote-only release para makuha ang per-table na gastos.
        def _rel_trades_only():
            trades, _ = _rows(bridge, args.rows, 0)
            tid, _ = _insert_copy(bridge, engine, trades, [])
            timer.reset()
            _release_current(bridge, engine, trades, [], tid, (), notify=False)
        def _rel_quotes_only():
            _, quotes = _rows(bridge, 0, args.quotes)
            _, qid = _insert_copy(bridge, engine, [], quotes)
            timer.reset()
            _release_current(bridge, engine, [], quotes, (), qid, notify=False)
        _measure(f"[{tag}] release/trades-only(no-notify)", _rel_trades_only)
        _measure(f"[{tag}] release/quotes-only(no-notify)", _rel_quotes_only)
        hot = _hot_stats(engine)
        report["variants"][f"[{tag}] hot_update_ratio"] = {
            t: (h / u if u else None) for t, (u, h) in hot.items()}
        print(f"  {tag} HOT-update ratio: " + ", ".join(
            f"{t}={(h / u if u else float('nan')):.2f} ({h}/{u})" for t, (u, h) in hot.items()))
    _teardown(engine)

    # Steady-state ceiling from the measured current path (insert+release, ff100).
    cur = report["variants"]
    ins = cur["[ff100(prod)] insert/current"]["wall_ms"] / 1e3
    rel = cur["[ff100(prod)] release/update+notify"]["wall_ms"] / 1e3
    model = DrainCostModel(fixed_s=0.0, per_event_s=(ins + rel) / (args.rows + args.quotes))
    report["ceiling_events_per_s_at_3600"] = model.steady_state_capacity(3600)
    print(f"\nmeasured current path: {ins + rel:.2f}s per {args.rows + args.quotes} events -> "
          f"ceiling at CATCHUP=3600: {model.steady_state_capacity(3600):.0f} events/s")
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database-url", default=None)
    ap.add_argument("--rows", type=int, default=1800)
    ap.add_argument("--quotes", type=int, default=1800)
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--json", default=None, help="write the report here")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
