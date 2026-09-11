"""EXIT VERDICT G -- no look-ahead (2026-09-10, [21]/[44]/[47] + Amendments).

Every tape read the verdict makes is symbol-scoped, as-of bounded on `observed_at`, bounded
on the bridge's DELIVERY stamp (`available_at`), id-tie-stable, and anchored on ONE `as_of`
per tick; the batch read resumes strictly after the FRONTIER TUPLE (the last walked print);
the since-high read has no LIMIT (n = len(rows), no count-then-LIMIT race) and resumes
strictly after the high print's tuple. A synthetic tape with prints AFTER the tick's as_of
yields byte-identical decisions, levels and receipts. No new helper touches the wall clock.
The `_FakeDB` pattern is tests/test_reentry_bar_is_the_tape.py:195-211.

Runnable: pytest tests/test_exit_verdict_f_no_lookahead.py -v   (DB-free)
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta

from app.services.trading.momentum_neural import entry_gates as EG
from app.services.trading.momentum_neural import exit_verdict as EV
from app.services.trading.momentum_neural import live_runner as lr

from tests.test_exit_verdict_f_state_machine import (
    Env,
    FakeTape,
    T_ENTRY,
    _le,
    _quiet_tape,
    _spike_sells,
    _spike_tape,
    _tick,
)

T0 = datetime(2026, 9, 10, 14, 0, 0)
T = datetime(2026, 9, 10, 14, 0, 37)


class _FakeDB:
    """Records every (sql, params); answers each read with a row of the shape it expects."""

    def __init__(self):
        self.statements: list[tuple[str, dict]] = []

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.statements.append((sql, dict(params or {})))
        if "EXTRACT(EPOCH FROM observed_at), observed_at, id" in sql:
            rows = [(10.4, 100, 10.39, 10.41, 1.8e9, T0 + timedelta(seconds=31), 778)]
        elif "statement_timeout" in sql:
            rows = []
        else:
            rows = [(10.4, 100, 10.39, 10.41, 1.8e9)]

        class _R:
            def fetchall(self_inner):
                return rows

            def fetchone(self_inner):
                return rows[0] if rows else None

        return _R()

    def begin_nested(self):
        import contextlib

        return contextlib.nullcontext()


def _reads(db: _FakeDB) -> list[tuple[str, dict]]:
    return [(s, p) for s, p in db.statements if "statement_timeout" not in s]


def _the_reads_of_one_tick(db: _FakeDB, as_of):
    EG.leg_prints_between("SKYQ", db=db, after=T0 + timedelta(seconds=34), after_id=790, as_of=as_of)
    EG.leg_prints_since_high("SKYQ", db=db, hi_at=T0 + timedelta(seconds=30), hi_id=777, as_of=as_of)
    EG.signed_tape_accel_features("SKYQ", db=db, window_prints=255, as_of=as_of)


def test_every_verdict_sql_is_symbol_scoped_as_of_bounded_delivery_bounded_and_tie_stable():
    db = _FakeDB()
    _the_reads_of_one_tick(db, T)
    reads = _reads(db)
    assert len(reads) == 3
    for sql, params in reads:
        assert "symbol = :s" in sql and params["s"] == "SKYQ"
        assert "observed_at <= :as_of" in sql
        if "LIMIT :n" in sql:
            # the N-print window read: the delivery bound is its OWN parameter (the base
            # read at the fill binds it to the tick); unset, it is the same instant as as_of
            assert "available_at IS NULL OR available_at <= :available_by" in sql
            assert params["available_by"] == T
        else:
            assert "available_at IS NULL OR available_at <= :as_of" in sql
        assert "observed_at ASC, id ASC" in sql            # id-tie-stable ordering on every read
        assert "make_interval" not in sql, "a print window is not a clock"
        assert params["as_of"] == T


def test_the_since_high_read_has_no_limit_and_resumes_strictly_after_the_high_print():
    db = _FakeDB()
    EG.leg_prints_since_high("SKYQ", db=db, hi_at=T0 + timedelta(seconds=30), hi_id=777, as_of=T)
    sql, params = _reads(db)[0]
    assert "LIMIT" not in sql
    assert "(observed_at, id) > (:hi_at, :hi_id)" in sql
    assert params["hi_id"] == 777 and params["hi_at"] == T0 + timedelta(seconds=30)


def test_the_batch_read_resumes_on_the_frontier_tuple_and_on_the_fill_when_there_is_no_tuple_yet():
    db = _FakeDB()
    EG.leg_prints_between("SKYQ", db=db, after=T0 + timedelta(seconds=34), after_id=790, as_of=T)
    sql, params = _reads(db)[0]
    assert "(observed_at, id) > (:after, :after_id)" in sql and params["after_id"] == 790
    assert "LIMIT" not in sql                                # every print, or none (unreadable)
    db = _FakeDB()
    EG.leg_prints_between("SKYQ", db=db, after=T0, as_of=T)   # the first pass: after the FILL
    sql, params = _reads(db)[0]
    assert "observed_at > :after" in sql and params["after"] == T0 and "after_id" not in params


def test_the_max_read_is_gone_the_walk_finds_the_high():
    """The leg high is found BY THE WALK (first occurrence, strictly greater) so no print can
    fall between a separate max() read and the batch; the reader was retired."""
    assert not hasattr(EG, "leg_high_print_first")
    src = inspect.getsource(lr._exit_verdict_tick)
    assert "leg_high_print_first" not in src and "_ev_leg_high_print(" not in src
    assert 'ev["leg_high"] = lh' in src and 'walk["leg_high"]' in src


def test_the_window_prints_branch_carries_the_delivery_bound_too():
    db = _FakeDB()
    EG.signed_tape_accel_features("SKYQ", db=db, window_prints=12, as_of=T)
    sql, params = _reads(db)[0]
    assert "LIMIT :n" in sql and params["n"] == 12
    assert "available_at IS NULL OR available_at <= :available_by" in sql
    assert params["available_by"] == params["as_of"] == T
    assert "ORDER BY observed_at DESC, id DESC" in sql


def test_the_delivery_bound_can_sit_after_the_observation_bound_but_never_before_it():
    db = _FakeDB()
    EG.signed_tape_accel_features("SKYQ", db=db, window_prints=12, as_of=T,
                                  available_by=T + timedelta(seconds=3.19))
    _sql, params = _reads(db)[0]
    assert params["as_of"] == T and params["available_by"] == T + timedelta(seconds=3.19)
    db = _FakeDB()
    EG.signed_tape_accel_features("SKYQ", db=db, window_prints=12, as_of=T,
                                  available_by=T - timedelta(seconds=5))
    _sql, params = _reads(db)[0]
    assert params["available_by"] == T       # clamped: a bound before as_of is a bug, not a window


def test_one_as_of_per_tick_under_the_replay_clock():
    """With no as_of threaded, every read anchors on the sim clock -- the same instant."""
    db = _FakeDB()
    with lr.replay_clock(T):
        EG.leg_prints_between("SKYQ", db=db, after=T0 + timedelta(seconds=34))
        EG.leg_prints_since_high("SKYQ", db=db, hi_at=T0 + timedelta(seconds=30), hi_id=777)
        EG.signed_tape_accel_features("SKYQ", db=db, window_prints=255)
    for _sql, params in _reads(db):
        assert params["as_of"] == T


def test_crypto_and_bad_bounds_fail_open_without_a_read():
    db = _FakeDB()
    assert EG.leg_prints_between("BTC-USD", db=db, after=T0, as_of=T) is None
    assert EG.leg_prints_since_high("BTC-USD", db=db, hi_at=T0, hi_id=1, as_of=T) is None
    assert EG.leg_prints_between("SKYQ", db=db, after=T, as_of=T0) is None            # as_of < after
    assert EG.leg_prints_since_high("SKYQ", db=db, hi_at=T0, hi_id=None, as_of=T) is None
    assert EG.leg_prints_between("SKYQ", db=None, after=T0, as_of=T) is None
    assert db.statements == []


# ── future prints are invisible: byte-identical decision, level and receipts ───

def _run(monkeypatch, tape: FakeTape):
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0)
    outs = [_tick(env, le, seconds=s) for s in (4.0, 12.5)]
    return env, le, outs


def test_prints_after_the_ticks_as_of_change_nothing(monkeypatch):
    clean = _spike_tape()
    _spike_sells(clean)
    polluted = _spike_tape()
    _spike_sells(polluted)
    # a violent future: a new leg high, then a crash below every level -- all AFTER 12.5 s
    polluted.add(13.0, 12.00, 5000, aggressor=1)
    polluted.add(14.0, 8.00, 5000, aggressor=-1)
    polluted.add(15.0, 7.50, 5000, aggressor=-1)
    env_a, le_a, out_a = _run(monkeypatch, clean)
    env_b, le_b, out_b = _run(monkeypatch, polluted)
    assert out_a[1]["action"] == "accel_rollover"
    assert out_a == out_b
    assert le_a["exit_verdict"] == le_b["exit_verdict"]
    assert env_a.emitted == env_b.emitted


def test_the_walk_sees_only_prints_up_to_as_of(monkeypatch):
    tape = _quiet_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31.0)
    _tick(env, le, seconds=36.0)
    level = le["exit_verdict"]["deadman"]["level"]
    tape.add(50.0, level - 0.10, 500, aggressor=-1)     # a crossing print in the FUTURE
    out = _tick(env, le, seconds=45.0)
    assert out["action"] is None and out["n_batch"] == 0
    out = _tick(env, le, seconds=51.0)
    assert out["action"] == "tick_deadman"


# ── no wall clock anywhere on the path ─────────────────────────────────────────

NEW_HELPERS = (
    "_exit_verdict_tick", "_exit_verdict_receipt_base", "_exit_verdict_settings",
    "_exit_verdict_receipt", "_exit_verdict_unreadable", "_exit_verdict_supported",
    "_exit_verdict_entry_at", "_exit_verdict_active", "_exit_verdict_unsupported_binding",
)


def test_no_new_helper_reads_the_wall_clock_directly():
    assert "datetime.now(" not in inspect.getsource(EV) and "datetime.utcnow(" not in inspect.getsource(EV)
    for name in NEW_HELPERS:
        src = inspect.getsource(getattr(lr, name))
        assert "datetime.now(" not in src and "datetime.utcnow(" not in src, name
    for name in ("leg_prints_between", "leg_prints_since_high"):
        src = inspect.getsource(getattr(EG, name))
        assert "datetime.now(" not in src and "datetime.utcnow(" not in src, name
        assert "_tape_asof_default(as_of)" in src, name


def test_the_tick_captures_one_as_of_and_threads_it_into_the_verdict():
    src = inspect.getsource(lr.tick_live_session)
    i = src.find("tick_as_of = _utcnow()")
    j = src.find("_exit_verdict_tick(")
    assert 0 < i < j
    call = src[j: j + 400]
    assert "as_of=tick_as_of" in call
    # the as-of is captured right after the held quote resolves (before the HWM ratchet)
    assert i < src.find('pos["high_water_mark"] = _hwm')
    # inside the machine every read carries the SAME as_of
    tick = inspect.getsource(lr._exit_verdict_tick)
    for read in ("_leg_between(", "_leg_since_high(", "_tape_feats("):
        k = tick.find(read)
        assert k > 0, read
        assert "as_of=" in tick[k: k + 260], read
