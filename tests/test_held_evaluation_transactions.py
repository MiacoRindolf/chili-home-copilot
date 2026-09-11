"""Real PostgreSQL SAVEPOINT and parent ORM state, using temporary tables only.

No app startup, migrations, live schemas, db fixture, or broker transport.
"""
from copy import deepcopy
from datetime import timedelta
import os

import pytest
from sqlalchemy import Column, Integer, JSON, create_engine, text
from sqlalchemy.orm import Session, declarative_base

from app.services.trading.momentum_neural import held_evaluation_audit as audit
from app.services.trading.momentum_neural import live_runner as lr
from tests.test_exit_verdict_f_state_machine import Env, T_ENTRY, PROD, _le, _quiet_tape

_REAL_EMIT = lr._emit
_REAL_COMMIT = lr._commit_le
Base = declarative_base()


class Parent(Base):
    __tablename__ = "held_audit_test_parent"
    id = Column(Integer, primary_key=True)
    risk_snapshot_json = Column(JSON, nullable=False)
    symbol = "SKYQ"
    state = "live_entered"
    correlation_id = "local-test-only"


@pytest.fixture
def connection():
    url = os.environ["TEST_DATABASE_URL"]
    assert url.rsplit("/", 1)[-1].split("?", 1)[0].endswith("_test")
    engine = create_engine(url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT current_database()")).scalar().endswith("_test")
        conn.execute(text("CREATE TEMP TABLE held_audit_test_parent (id integer PRIMARY KEY, risk_snapshot_json json NOT NULL)"))
        conn.execute(text("CREATE TEMP TABLE trading_automation_events (id bigserial PRIMARY KEY, "
                          "session_id integer NOT NULL, ts timestamp NOT NULL, event_type varchar(64) NOT NULL, "
                          "payload_json jsonb NOT NULL, correlation_id varchar(64), source_node_id varchar(80))"))
        conn.commit()  # temp tables survive the parent rollback we intentionally test
        yield conn
        conn.rollback()
    engine.dispose()


def seeded(connection):
    db = Session(bind=connection, autoflush=False, expire_on_commit=False)
    le = _le()
    parent = Parent(id=1, risk_snapshot_json={lr.KEY_LIVE_EXEC: deepcopy(le)})
    db.add(parent)
    db.commit()
    return db, parent, le


def reject_receipts(connection):
    connection.execute(text("ALTER TABLE trading_automation_events ADD CONSTRAINT reject_held_receipt "
                            "CHECK(event_type <> 'live_exit_evaluation')"))
    connection.commit()


def clocks(db):
    return tuple(db.execute(text("SELECT current_setting('statement_timeout'), current_setting('lock_timeout')")).one())


@pytest.mark.parametrize("fails", [False, True])
def test_optional_inner_session_never_preflushes_parent_or_leaks_timeout(connection, fails):
    if fails:
        reject_receipts(connection)
    with seeded(connection)[0] as db:
        parent = db.get(Parent, 1)
        parent.risk_snapshot_json = {"decided_exit": "whole_position", "quantity": 10}
        db.execute(text("SET LOCAL statement_timeout='13s'"))
        db.execute(text("SET LOCAL lock_timeout='11s'"))
        before = clocks(db)
        seen = []

        def emit(child, sess, name, payload):
            # Same physical connection, separate Session/savepoint. The dirty decision
            # is not prematurely flushed merely to try optional instrumentation.
            assert child.connection() is db.connection()
            assert parent in db.dirty
            assert child.execute(text("SELECT risk_snapshot_json FROM held_audit_test_parent WHERE id=1")).scalar().get("decided_exit") is None
            seen.append(clocks(child))
            return _REAL_EMIT(child, sess, name, payload)

        if fails:
            with pytest.raises(Exception) as error:
                audit._optional_emit(db, parent, {"test": True}, emit=emit, timeout_ms=271)
            assert "CheckViolation" in str(error.value)
        else:
            receipt = audit._optional_emit(db, parent, {"test": True}, emit=emit, timeout_ms=271)
            assert receipt.id is not None
        assert seen == [("271ms", "271ms")]
        assert clocks(db) == before
        assert parent in db.dirty and db.execute(text("SELECT 42")).scalar() == 42
        db.commit()
        assert db.execute(text("SELECT risk_snapshot_json FROM held_audit_test_parent WHERE id=1")).scalar()["decided_exit"] == "whole_position"
        count = db.execute(text("SELECT count(*) FROM trading_automation_events")).scalar()
        assert count == (0 if fails else 1)


def run_tick(db, parent, le, env, seconds=36):
    env.now = T_ENTRY + timedelta(seconds=seconds)
    return lr._exit_verdict_tick(db, parent, le, as_of=env.now, bid=10.1, ask=10.11,
                                mid=10.105, qty=10., avg=10., stop_px=9., prod=PROD)


def wire(monkeypatch, tape=None):
    env = Env(monkeypatch, tape=tape or _quiet_tape(), now=T_ENTRY)
    monkeypatch.setattr(lr, "_emit", _REAL_EMIT)
    monkeypatch.setattr(lr, "_commit_le", _REAL_COMMIT)
    return env


def test_outer_rollback_removes_receipt_and_parent_pointer_including_repeated_attempts(connection, monkeypatch):
    env = wire(monkeypatch)
    db, parent, le = seeded(connection)
    with db:
        run_tick(db, parent, le, env)
        run_tick(db, parent, le, env)
        pointer = le["exit_verdict"]["evaluation_audit"]["previous_feature"]
        assert pointer["event_id"] is not None
        assert db.execute(text("SELECT count(*) FROM trading_automation_events WHERE event_type='live_exit_evaluation'")).scalar() == 2
        db.rollback()
        assert db.execute(text("SELECT count(*) FROM trading_automation_events")).scalar() == 0
        assert "evaluation_audit" not in le["exit_verdict"]
        assert "exit_verdict" not in parent.risk_snapshot_json[lr.KEY_LIVE_EXEC]
        assert "held_audit_parent_pointers" not in db.info


def test_committed_parent_keeps_receipt_but_later_rollback_restores_committed_link(connection, monkeypatch):
    env = wire(monkeypatch)
    db, parent, le = seeded(connection)
    with db:
        run_tick(db, parent, le, env)
        first = deepcopy(le["exit_verdict"]["evaluation_audit"])
        db.commit()
        assert "held_audit_parent_pointers" not in db.info
        run_tick(db, parent, le, env)
        assert le["exit_verdict"]["evaluation_audit"]["last_attempt"] != first["last_attempt"]
        db.rollback()
        assert le["exit_verdict"]["evaluation_audit"] == first
        assert db.execute(text("SELECT count(*) FROM trading_automation_events WHERE event_type='live_exit_evaluation'")).scalar() == 1
        assert parent.risk_snapshot_json[lr.KEY_LIVE_EXEC]["exit_verdict"]["evaluation_audit"] == first


def test_callers_own_savepoint_rollback_restores_only_its_receipt_pointer(connection, monkeypatch):
    env = wire(monkeypatch)
    db, parent, le = seeded(connection)
    with db:
        run_tick(db, parent, le, env)
        first = deepcopy(le["exit_verdict"]["evaluation_audit"])
        with db.begin_nested() as parent_savepoint:
            run_tick(db, parent, le, env)
            assert le["exit_verdict"]["evaluation_audit"] != first
            parent_savepoint.rollback()
        assert le["exit_verdict"]["evaluation_audit"] == first
        assert db.execute(text("SELECT count(*) FROM trading_automation_events WHERE event_type='live_exit_evaluation'")).scalar() == 1
        with db.begin_nested() as unrelated_savepoint:
            db.execute(text("SELECT 42"))
            unrelated_savepoint.rollback()
        assert le["exit_verdict"]["evaluation_audit"] == first
        db.rollback()
        assert "evaluation_audit" not in le["exit_verdict"]
        assert db.execute(text("SELECT count(*) FROM trading_automation_events")).scalar() == 0


def test_real_receipt_insert_fault_does_not_rollback_decided_whole_exit(connection, monkeypatch):
    reject_receipts(connection)
    tape = _quiet_tape()
    tape.add(35., 8.5, 100, aggressor=-1)
    env = wire(monkeypatch, tape)
    db, parent, le = seeded(connection)
    with db:
        decision = run_tick(db, parent, le, env)
        assert decision["action"] == "tick_deadman" and decision["phase"] == "exit_pending"
        assert parent in db.dirty
        assert le["exit_verdict"]["evaluation_audit"]["last_attempt"]["status"] == "receipt_missing"
        assert le["position"]["quantity"] == 10
        assert le["deadman_stop"]["order_id"] == "dm-oid-1"
        # The caller can persist its already-decided sell intent using the still-usable
        # outer transaction. This seam test does not claim an actual broker submission.
        le["pending_exit_reason"] = le["exit_verdict"]["exit"]["reason"]
        _REAL_COMMIT(parent, le)
        db.commit()
        saved = db.execute(text("SELECT risk_snapshot_json FROM held_audit_test_parent WHERE id=1")).scalar()[lr.KEY_LIVE_EXEC]
        assert saved["pending_exit_reason"] == "tick_deadman_stop"
        assert saved["exit_verdict"]["exit"]["exit_fraction"] == 1.
        assert saved["deadman_stop"]["qty"] == 10
        assert db.execute(text("SELECT count(*) FROM trading_automation_events WHERE event_type='live_tick_deadman_exit'")).scalar() == 1
        assert db.execute(text("SELECT count(*) FROM trading_automation_events WHERE event_type='live_exit_evaluation'")).scalar() == 0


def test_actual_tick_links_same_query_g_membership_and_separate_digest_only_reads(connection, monkeypatch):
    # Every clock and row here is fixture data; no live market/data provider.
    connection.execute(text("CREATE TEMP TABLE iqfeed_trade_ticks (id bigint, symbol text, "
                            "observed_at timestamp, received_at timestamptz, available_at timestamptz, "
                            "price float8, size float8, bid float8, ask float8)"))
    for row in _quiet_tape().rows:
        price, size, bid, ask, _, observed, idx = row
        connection.execute(text("INSERT INTO iqfeed_trade_ticks VALUES (:id,'SKYQ',:event, "
                                ":event AT TIME ZONE 'UTC',:event AT TIME ZONE 'UTC',:price,:size,:bid,:ask)"),
                           {"id": idx, "event": observed, "price": price, "size": size, "bid": bid, "ask": ask})
    connection.commit()
    # Keep all three real readers. Only non-I/O scheduling and strategy clock are fakes.
    from types import SimpleNamespace
    env = SimpleNamespace(now=T_ENTRY)
    monkeypatch.setattr(lr, "_utcnow", lambda: env.now)
    monkeypatch.setattr(lr, "_schedule_exit_continuation", lambda *a: True)
    db, parent, le = seeded(connection)
    with db:
        out = run_tick(db, parent, le, env)
        assert out["action"] is None
        payload = db.execute(text("SELECT payload_json FROM trading_automation_events WHERE event_type='live_exit_evaluation'")).scalar_one()
        assert [r["role"] for r in payload["reads"]] == ["walk", "entry_base", "G", "D"]
        walk, base, current, d = payload["reads"]
        assert all(r["status"] == "success" for r in payload["reads"])
        assert walk["membership_recovery"] == d["membership_recovery"] == "digest_only"
        assert "ordered_ids" not in walk and "ordered_ids" not in d
        assert base["ordered_ids"] == [10000, 10001, 10002, 10003]
        assert current["ordered_ids"] == list(range(10000, 10011))
        assert current["feature_contract"] == "legacy_time_split"
        assert payload["observations"]["G_features"]["signed_tape_accel"] == out["accel_now"]
        assert le["exit_verdict"]["evaluation_audit"]["previous_feature"]["read_id"] == current["read_id"]
        assert le["exit_verdict"]["deadman"]["base_observation"]["read_id"] == base["read_id"]
        assert len(set(r["read_id"] for r in payload["reads"])) == 4
        assert payload["common_prefix_id"] is None
        db.rollback()
        assert "base_observation" not in le["exit_verdict"]["deadman"]
