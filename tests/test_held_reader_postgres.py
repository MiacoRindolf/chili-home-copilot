"""Actual held evaluator + original role SQL on a UUID-owned test schema.

No app startup, migration or db/client fixture. The cache is explicitly injected;
pytest never enables the production physical-reader factory. All clocks/prints
are synthetic; this is transaction/branch evidence, not a trading improvement.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta, timezone
import os
import re
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.services.trading.momentum_neural import entry_gates as eg
from app.services.trading.momentum_neural import held_evaluation_audit as audit
from app.services.trading.momentum_neural import held_market_snapshot as snapshot
from app.services.trading.momentum_neural import live_runner as lr
from tests.test_exit_verdict_f_state_machine import T_ENTRY, PROD, _le


@pytest.fixture
def market():
    url = os.environ["TEST_DATABASE_URL"]
    database = url.rsplit("/", 1)[-1].split("?", 1)[0]
    assert database.endswith("_test") and database not in {"chili", "chili_test"}
    schema = "held_rr_build_" + uuid4().hex
    assert re.fullmatch(r"held_rr_build_[0-9a-f]{32}", schema)
    engine = create_engine(url, poolclass=NullPool, connect_args={
        "application_name": "astra-held-reader-fixture", "connect_timeout": 20,
        "options": f"-c statement_timeout=20000 -c search_path={schema}",
    })
    created = False
    manager = None
    try:
        with engine.begin() as conn:
            assert conn.execute(text("SELECT current_database()")).scalar_one() == database
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            conn.execute(text(f'CREATE TABLE "{schema}".iqfeed_trade_ticks ('
                              "id bigint PRIMARY KEY, symbol text, observed_at timestamp, "
                              "received_at timestamptz, available_at timestamptz, "
                              "price float8, size float8, bid float8, ask float8)"))
        created = True

        def factory():
            return engine.connect()

        manager = snapshot.ReaderCache(factory, capacity=1, budget={"reader_capacity": 1, "fixture": True},
                                       health_timeout_ms=2000, shutdown_wait_s=2)
        manager.start()

        def put(identifier, offset, price, *, size=100, buy=True, symbol="SKYQ"):
            observed = T_ENTRY + timedelta(seconds=offset)
            received = observed.replace(tzinfo=timezone.utc) + timedelta(milliseconds=10)
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO iqfeed_trade_ticks VALUES "
                                  "(:id,:symbol,:observed,:received,:available,:price,:size,:bid,:ask)"),
                             {"id": identifier, "symbol": symbol, "observed": observed, "received": received,
                              "available": received + timedelta(milliseconds=10), "price": price,
                              "size": size, "bid": price - .01 if buy else price,
                              "ask": price if buy else price + .01})

        yield SimpleNamespace(engine=engine, manager=manager, put=put, schema=schema)
    finally:
        if manager is not None:
            manager.stop()
            if manager.thread is not None:
                manager.thread.join(2)
            assert not manager.slots, "Reader resources leaked at test teardown"
        if created:
            with engine.begin() as conn:
                # Exact UUID-owned namespace only; never an application schema.
                conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


@contextmanager
def admitted(market, *, session_id=21605):
    deadline = time.monotonic() + 3
    lease = None
    while time.monotonic() < deadline:
        lease, _ = market.manager.try_lease()
        if lease is not None:
            break
        time.sleep(.001)
    assert lease is not None, "Real reader did not finish administrative warm-up"
    admission = {"lease": lease, "reason": None, "ordinary_session_id": session_id,
                 "budget": market.manager.budget}
    token = snapshot._ADMISSION.set(admission)
    try:
        yield lease
    finally:
        snapshot._ADMISSION.reset(token)
        lease.release()


def seed(market, *, sellers=False, cross=False):
    rows = [(-4, 9.85), (-3, 9.90), (-2, 9.95), (-1, 9.98),
            (1, 10.20), (2, 10.19), (3, 10.18), (4, 10.19), (5, 8.90 if cross else 10.18)]
    for identifier, (offset, price) in enumerate(rows, 1):
        market.put(identifier, offset, price, buy=not (sellers and identifier >= 5))


def wire(monkeypatch):
    emitted = []

    def emit(db, sess, kind, payload):
        emitted.append((kind, deepcopy(payload)))
        return SimpleNamespace(id=len(emitted))

    monkeypatch.setattr(lr, "_emit", emit)
    monkeypatch.setattr(lr, "_commit_le", lambda sess, le: setattr(sess, "risk_snapshot_json", {lr.KEY_LIVE_EXEC: deepcopy(le)}))
    return emitted


def tick(db, le, *, seconds=6):
    sess = SimpleNamespace(id=21605, symbol="SKYQ", execution_family="alpaca_spot", state="live_entered")
    result = lr._exit_verdict_tick(db, sess, le, as_of=T_ENTRY + timedelta(seconds=seconds),
                                 bid=10.18, ask=10.19, mid=10.185, qty=10., avg=10., stop_px=9., prod=PROD)
    return result, sess


def evaluation(emitted):
    return next(payload for kind, payload in reversed(emitted) if kind == "live_exit_evaluation")


@pytest.mark.parametrize("coherent", [False, True], ids=["RC-control", "preadmitted-RR"])
def test_actual_evaluator_all_four_role_memberships_and_commits_between_roles(market, monkeypatch, coherent):
    seed(market)
    emitted = wire(monkeypatch)
    original = audit.QueryObservation.returned
    membership = {}

    def observed(self, rows=None, error=None):
        original(self, rows=rows, error=error)
        if error is None:
            role = self.record["role"]
            membership[role] = [int(row[5 if self.exact_n is not None else 6]) for row in rows]
            if role == "walk":
                market.put(11, -.5, 9.96)  # eligible historical row commits after walk
            if role == "G":
                market.put(10, 5.5, 10.10)  # eligible suffix row commits after G

    monkeypatch.setattr(audit.QueryObservation, "returned", observed)
    le = _le()
    if coherent:
        with admitted(market) as lease, Session(market.engine, autoflush=False) as db:
            result, _ = tick(db, le)
            assert not lease.slot.connection.in_transaction()
    else:
        with Session(market.engine, autoflush=False) as db:
            result, _ = tick(db, le)
    assert result["action"] is None
    assert membership["walk"] == [5, 6, 7, 8, 9]
    assert membership["entry_base"] == ([1, 2, 3, 4] if coherent else [1, 2, 3, 4, 11])
    assert membership["G"] == ([1, 2, 3, 4, 5, 6, 7, 8, 9] if coherent else [1, 2, 3, 4, 11, 5, 6, 7, 8, 9])
    assert membership["D"] == ([6, 7, 8, 9] if coherent else [6, 7, 8, 9, 10])
    payload = evaluation(emitted)
    assert [r["role"] for r in payload["reads"]] == ["walk", "entry_base", "G", "D"]
    mode = payload["market_snapshot"]
    assert mode["common_snapshot"] is coherent
    assert payload["captured_prefix"] is payload["replay_authority"] is False
    assert payload["quote_read_identity"] is None
    if coherent:
        assert mode["successful_role_reads"] == 4 and mode["cleanup"] == "transaction_ended"
        assert {r["market_snapshot"]["epoch_id"] for r in payload["reads"]} == {mode["epoch_id"]}
        assert mode["historical_state_is_in_current_epoch"] is False
    else:
        assert mode["reason"] == "reader_not_preadmitted" and mode["epoch_id"] is None
    assert all("ordered_ids" not in r for r in payload["reads"] if r["role"] in {"walk", "D"})


@pytest.mark.parametrize("fault", ["sql_error", "timeout"])
def test_actual_D_fault_retains_G_advancement_and_outer_RR_then_closes(market, monkeypatch, fault):
    seed(market)
    emitted = wire(monkeypatch)
    le = _le()
    with admitted(market) as lease, Session(market.engine, autoflush=False) as db:
        connection = lease.slot.connection
        before_timeout = "20s"

        def inject(conn, cursor, statement, params, context, many):
            if "hi_at" in statement and "FROM iqfeed_trade_ticks" in statement:
                return ("SELECT pg_sleep(3)" if fault == "timeout" else "SELECT absent_probe_column", {})
            return statement, params

        event.listen(connection, "before_cursor_execute", inject, retval=True)
        result, _ = tick(db, le)
        assert result["action"] is None
        assert not connection.in_transaction()
        assert le["exit_verdict"]["frontier_id"] == 9
        assert le["exit_verdict"]["accel_prev"] is not None
        assert le["exit_verdict"]["evaluation_audit"]["previous_feature"]["value"] == le["exit_verdict"]["accel_prev"]
        with connection.begin():
            assert connection.execute(text("SHOW statement_timeout")).scalar_one() == before_timeout
        # The explicit verification transaction is not part of the earlier receipt.
    payload = evaluation(emitted)
    assert payload["read_roles"]["D"]["status"] == "query_error"
    assert payload["read_roles"]["G"]["status"] == "query_success"
    assert payload["feature_pointer_advanced"] is True
    assert payload["market_snapshot"]["common_snapshot"] is True
    assert payload["market_snapshot"]["cleanup"] == "transaction_ended"


@pytest.mark.parametrize("trigger", ["deadman", "G"])
def test_actual_protective_priority_never_executes_later_optional_D(market, monkeypatch, trigger):
    seed(market, sellers=trigger == "G", cross=trigger == "deadman")
    emitted = wire(monkeypatch)
    le = _le()
    if trigger == "G":
        le["exit_verdict"] = {"phase": "armed", "accel_prev": 1200.,
                              "accel_prev_contract": lr._exit_verdict_settings()["contract_id"],
                              "frontier_at": T_ENTRY.isoformat(), "frontier_id": None,
                              "deadman": {"level": 9., "level_source": "resting_stop", "ratchets": 0}}
    with admitted(market) as lease, Session(market.engine, autoflush=False) as db:
        observed_statements = []

        def guard(conn, cursor, statement, params, context, many):
            if "FROM iqfeed_trade_ticks" in statement:
                observed_statements.append(statement)
                assert "hi_at" not in statement, "D must not delay a prior protective decision"
                if trigger == "deadman" and "LIMIT" in statement:
                    assert params.get("as_of") == T_ENTRY, "Current G must not run after a crossing"

        event.listen(lease.slot.connection, "before_cursor_execute", guard)
        # Deliberately stale crossing still decides; the RR layer cannot withhold it.
        result, _ = tick(db, le, seconds=100 if trigger == "deadman" else 6)
        assert result["action"] == ("tick_deadman" if trigger == "deadman" else "accel_rollover")
        assert not lease.slot.connection.in_transaction()
        assert le["exit_verdict"]["exit"]["exit_fraction"] == 1.
    payload = evaluation(emitted)
    assert payload["read_roles"]["D"]["status"] == "not_reached"
    if trigger == "deadman":
        assert payload["read_roles"]["G"]["status"] == "not_reached" and result["stale"] is True
    else:
        assert payload["read_roles"]["G"]["status"] == "query_success"
    assert payload["market_snapshot"]["cleanup"] == "transaction_ended"
    assert len(observed_statements) == 2
