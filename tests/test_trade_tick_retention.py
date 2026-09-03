"""iqfeed_trade_ticks retention — bounded PRIMARY-KEY-RANGE prune (2026-09-03).

Sinukat: 94 GB / 248M rows, WALANG retention (pinakaluma 07-18), sira ang
observed_at BRIN kaya oras ang time-predicate scan. Ang prune ay (1) naghahanap ng
cutoff id sa pamamagitan ng bounded pk bisection, (2) nagbubura sa 200k-id na
batch na kani-kaniyang commit, (3) BOUNDED bawat tawag (max batches). Ang mga test
dito ay DB-free kung saan posible (fake probe / fake session); dalawa lang ang
tumatama sa chili_test (ang tunay na DELETE at ang reloptions ng mig375).

Runnable (mag-isa, hindi ang buong suite):
    pytest tests/test_trade_tick_retention.py -v -p no:cacheprovider
"""
from __future__ import annotations

import ast
import inspect
import logging
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import migrations as mg
from app.config import Settings, settings
from app.services import trading_scheduler
from app.services.trading.momentum_neural import trade_tick_retention as ttr
from app.services.trading.momentum_neural.trade_tick_retention import (
    TABLE,
    bisect_cutoff_id,
    plan_batches,
    prune_trade_ticks,
    retention_knobs,
)

_T0 = datetime(2026, 9, 3, 0, 0, 0)  # naive UTC, like the tape column


# ── fake cursor: rows as (id, observed_at); probe = first row with id >= i ──────
class _FakeTape:
    def __init__(self, rows: list[tuple[int, datetime]]) -> None:
        self.rows = sorted(rows)
        self.probe_args: list[int] = []

    def probe(self, i: int):
        self.probe_args.append(int(i))
        for rid, ts in self.rows:
            if rid >= i:
                return ts
        return None

    @property
    def lo(self) -> int:
        return self.rows[0][0]

    @property
    def hi(self) -> int:
        return self.rows[-1][0]


def _rows(ids: list[int], *, step_minutes: int = 1) -> list[tuple[int, datetime]]:
    return [(i, _T0 + timedelta(minutes=k * step_minutes)) for k, i in enumerate(ids)]


# ── pure: cutoff bisection ───────────────────────────────────────────────────
def test_bisect_finds_first_retained_id_across_gaps() -> None:
    tape = _FakeTape(_rows([10, 11, 13, 20, 21, 35, 40]))
    # ids 10,11,13,20 are minutes 0..3; cutoff between minute 3 (id 20) and 4 (id 21)
    cutoff = _T0 + timedelta(minutes=3, seconds=30)
    cutoff_id, boundary, probes = bisect_cutoff_id(tape.probe, lo=tape.lo, hi=tape.hi, cutoff=cutoff)
    assert cutoff_id == 21
    assert boundary == _T0 + timedelta(minutes=4)
    assert probes <= 64
    assert all(tape.lo <= a <= tape.hi for a in tape.probe_args), tape.probe_args
    # every id below the cutoff is old, every id at/after it is retained
    assert all(ts < cutoff for rid, ts in tape.rows if rid < cutoff_id)
    assert all(ts >= cutoff for rid, ts in tape.rows if rid >= cutoff_id)


def test_bisect_row_exactly_at_cutoff_is_retained() -> None:
    tape = _FakeTape(_rows([1, 2, 3, 4]))
    cutoff = _T0 + timedelta(minutes=2)  # == observed_at of id 3
    cutoff_id, boundary, _ = bisect_cutoff_id(tape.probe, lo=1, hi=4, cutoff=cutoff)
    assert cutoff_id == 3 and boundary == cutoff


def test_bisect_nothing_older_than_cutoff_returns_lo() -> None:
    tape = _FakeTape(_rows([100, 101, 102]))
    cutoff = _T0 - timedelta(days=1)
    cutoff_id, boundary, probes = bisect_cutoff_id(tape.probe, lo=100, hi=102, cutoff=cutoff)
    assert cutoff_id == 100 and boundary == _T0 and probes == 1
    assert plan_batches(100, cutoff_id, batch_ids=10, max_batches=5) == []


def test_bisect_everything_older_returns_hi_plus_one() -> None:
    tape = _FakeTape(_rows([5, 6, 7, 9]))
    cutoff = _T0 + timedelta(days=30)
    cutoff_id, boundary, probes = bisect_cutoff_id(tape.probe, lo=5, hi=9, cutoff=cutoff)
    assert cutoff_id == 10 and boundary is None and probes == 2
    assert plan_batches(5, cutoff_id, batch_ids=100, max_batches=5) == [(5, 10)]


def test_bisect_empty_range_is_a_noop() -> None:
    tape = _FakeTape([])
    cutoff_id, boundary, probes = bisect_cutoff_id(tape.probe, lo=1, hi=1, cutoff=_T0)
    assert cutoff_id == 1 and boundary is None and probes == 1


def test_bisect_normalizes_tz_aware_probe_and_cutoff() -> None:
    aware = [(i, (_T0 + timedelta(minutes=k)).replace(tzinfo=timezone.utc)) for k, i in enumerate([1, 2, 3, 4])]
    tape = _FakeTape(aware)
    cutoff = (_T0 + timedelta(minutes=1, seconds=30)).replace(tzinfo=timezone.utc)
    cutoff_id, _, _ = bisect_cutoff_id(tape.probe, lo=1, hi=4, cutoff=cutoff)
    assert cutoff_id == 3


def test_bisect_probe_count_is_logarithmic_on_a_248m_row_tape() -> None:
    """The measured table: 248M ids, one row per second-ish. Bisection must find the
    cutoff in ~28 probes — NEVER a scan proportional to the row count."""
    n = 248_000_000
    calls = {"n": 0}

    def probe(i: int):
        calls["n"] += 1
        if i > n:
            return None
        return _T0 + timedelta(seconds=max(1, i))

    cutoff = _T0 + timedelta(seconds=174_000_000)  # ~14d retained out of 47d
    cutoff_id, boundary, probes = bisect_cutoff_id(probe, lo=1, hi=n, cutoff=cutoff)
    assert cutoff_id == 174_000_000
    assert boundary == cutoff
    assert probes == calls["n"] <= 32


# ── pure: batch planning bounds ──────────────────────────────────────────────
def test_plan_batches_half_open_ranges_capped_by_max_batches() -> None:
    assert plan_batches(0, 1_000_000, batch_ids=200_000, max_batches=3) == [
        (0, 200_000), (200_000, 400_000), (400_000, 600_000),
    ]
    full = plan_batches(0, 1_000_000, batch_ids=200_000, max_batches=10)
    assert len(full) == 5 and full[-1] == (800_000, 1_000_000)
    # ranges tile [min, cutoff) exactly: contiguous, non-overlapping
    for (a_lo, a_hi), (b_lo, _b_hi) in zip(full, full[1:]):
        assert a_hi == b_lo and a_lo < a_hi


def test_plan_batches_last_partial_batch_and_degenerate_inputs() -> None:
    assert plan_batches(5, 17, batch_ids=5, max_batches=10) == [(5, 10), (10, 15), (15, 17)]
    assert plan_batches(17, 17, batch_ids=5, max_batches=10) == []
    assert plan_batches(20, 17, batch_ids=5, max_batches=10) == []
    # bad knobs are clamped to 1, never 0 (an unbounded/zero-width batch)
    assert plan_batches(0, 3, batch_ids=0, max_batches=0) == [(0, 1)]


# ── knob parsing ─────────────────────────────────────────────────────────────
class _Cfg:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_retention_knobs_defaults_when_missing_none_zero_or_garbage() -> None:
    assert retention_knobs(_Cfg()) == {"retention_days": 14, "batch_ids": 200_000, "max_batches": 50}
    assert retention_knobs(_Cfg(
        chili_momentum_trade_tick_retention_days=None,
        chili_momentum_trade_tick_prune_batch_ids=0,
        chili_momentum_trade_tick_prune_max_batches="garbage",
    )) == {"retention_days": 14, "batch_ids": 200_000, "max_batches": 50}


def test_retention_knobs_reads_overrides_and_clamps_to_one() -> None:
    k = retention_knobs(_Cfg(
        chili_momentum_trade_tick_retention_days="21",
        chili_momentum_trade_tick_prune_batch_ids=50_000,
        chili_momentum_trade_tick_prune_max_batches=-3,
    ))
    assert k == {"retention_days": 21, "batch_ids": 50_000, "max_batches": 1}


def test_retention_knobs_default_reads_the_runtime_settings(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_trade_tick_retention_days", 9, raising=False)
    assert retention_knobs()["retention_days"] == 9


def test_settings_fields_defaults_bounds_and_measured_rationale() -> None:
    f = Settings.model_fields
    days = f["chili_momentum_trade_tick_retention_days"]
    batch = f["chili_momentum_trade_tick_prune_batch_ids"]
    cap = f["chili_momentum_trade_tick_prune_max_batches"]
    flag = f["chili_momentum_trade_tick_prune_enabled"]
    assert days.default == 14 and batch.default == 200_000 and cap.default == 50
    assert flag.default is True  # kill-switch only, no dark flag
    # the WHY carries the measured numbers, not just a sentence
    d = str(days.description)
    assert "9100" in d and "180" in d and "94 GB" in d and "248M" in d
    assert "5.3M" in str(batch.description) and "5.3M" in str(cap.description)


def test_settings_env_aliases_parse_and_reject_out_of_range() -> None:
    s = Settings(
        _env_file=None,
        CHILI_MOMENTUM_TRADE_TICK_RETENTION_DAYS="21",
        CHILI_MOMENTUM_TRADE_TICK_PRUNE_BATCH_IDS="50000",
        CHILI_MOMENTUM_TRADE_TICK_PRUNE_MAX_BATCHES="3",
        CHILI_MOMENTUM_TRADE_TICK_PRUNE_ENABLED="0",
    )
    assert s.chili_momentum_trade_tick_retention_days == 21
    assert s.chili_momentum_trade_tick_prune_batch_ids == 50_000
    assert s.chili_momentum_trade_tick_prune_max_batches == 3
    assert s.chili_momentum_trade_tick_prune_enabled is False
    d = Settings(_env_file=None)
    assert (d.chili_momentum_trade_tick_retention_days, d.chili_momentum_trade_tick_prune_batch_ids,
            d.chili_momentum_trade_tick_prune_max_batches) == (14, 200_000, 50)
    for bad in ("0", "181"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, CHILI_MOMENTUM_TRADE_TICK_RETENTION_DAYS=bad)


# ── DB-free: prune against a fake session (skip / partial-failure paths) ───────
class _FakeSession:
    """Answers the exact statements prune_trade_ticks issues; optional failure on
    the Nth DELETE so the partial-progress reporting is exercised."""

    def __init__(self, rows: list[tuple[int, datetime]] | None, *, fail_on_delete: int | None = None):
        self.tape = _FakeTape(rows) if rows is not None else None
        self.fail_on_delete = fail_on_delete
        self.deletes: list[tuple[int, int]] = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, stmt, params=None):
        sql = str(stmt)
        params = params or {}
        if "to_regclass" in sql:
            return _Res(scalar=None if self.tape is None else TABLE)
        if "min(id), max(id)" in sql:
            if not self.tape.rows:
                return _Res(one=(None, None))
            return _Res(one=(self.tape.lo, self.tape.hi))
        if "ORDER BY id LIMIT 1" in sql:
            return _Res(scalar=self.tape.probe(params["probe"]))
        if sql.startswith("DELETE"):
            if self.fail_on_delete is not None and len(self.deletes) + 1 == self.fail_on_delete:
                raise RuntimeError("boom: simulated lock_timeout on batch")
            lo, hi = params["lo"], params["hi"]
            n = sum(1 for rid, _ in self.tape.rows if lo <= rid < hi)
            self.tape.rows = [r for r in self.tape.rows if not (lo <= r[0] < hi)]
            self.deletes.append((lo, hi))
            return _Res(rowcount=n)
        raise AssertionError(f"unexpected SQL: {sql}")

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _Res:
    def __init__(self, *, scalar=None, one=None, rowcount=0):
        self._scalar, self._one, self.rowcount = scalar, one, rowcount

    def scalar(self):
        return self._scalar

    def one(self):
        return self._one


def test_prune_skips_cleanly_when_the_bridge_table_is_absent() -> None:
    db = _FakeSession(None)
    out = prune_trade_ticks(db, retention_days=14)
    assert out["ok"] and out.get("skipped") == "no_table"
    assert out["deleted"] == 0 and out["batches"] == 0 and db.deletes == []


def test_prune_skips_cleanly_when_the_table_is_empty() -> None:
    db = _FakeSession([])
    out = prune_trade_ticks(db, retention_days=14)
    assert out["ok"] and out.get("skipped") == "empty" and db.deletes == []


def test_prune_bounded_by_max_batches_then_drains_on_the_next_call() -> None:
    now = _T0 + timedelta(days=40)
    # ids 1..8 are 40..33 days old (old), 9..12 are 1 day old (retained)
    rows = [(i, _T0 + timedelta(days=i - 1)) for i in range(1, 9)]
    rows += [(i, now - timedelta(days=1)) for i in range(9, 13)]
    db = _FakeSession(rows)

    first = prune_trade_ticks(db, retention_days=14, batch_ids=3, max_batches=2, now_utc=now)
    assert first["ok"] and first["cutoff_id"] == 9
    assert first["cutoff_observed_at"] == (now - timedelta(days=1)).isoformat(timespec="seconds")
    assert first["batches"] == 2 and first["deleted"] == 6
    assert first["exhausted"] is False and first["remaining_ids"] == 2
    assert db.deletes == [(1, 4), (4, 7)] and db.commits == 2  # one commit PER batch
    assert first["probes"] <= 64 and first["seconds"] >= 0.0

    second = prune_trade_ticks(db, retention_days=14, batch_ids=3, max_batches=2, now_utc=now)
    assert second["ok"] and second["batches"] == 1 and second["deleted"] == 2
    assert second["exhausted"] is True and second["remaining_ids"] == 0
    assert db.deletes[-1] == (7, 9)
    assert [rid for rid, _ in db.tape.rows] == [9, 10, 11, 12]

    third = prune_trade_ticks(db, retention_days=14, batch_ids=3, max_batches=2, now_utc=now)
    assert third["ok"] and third["batches"] == 0 and third["deleted"] == 0 and third["exhausted"]


def test_prune_failure_rolls_back_and_reports_committed_progress(caplog) -> None:
    now = _T0 + timedelta(days=40)
    rows = [(i, _T0 + timedelta(days=i - 1)) for i in range(1, 9)]
    db = _FakeSession(rows, fail_on_delete=2)
    with caplog.at_level(logging.WARNING, logger=ttr.__name__):
        out = prune_trade_ticks(db, retention_days=14, batch_ids=3, max_batches=5, now_utc=now)
    assert out["ok"] is False and "boom" in out["error"]
    assert out["batches"] == 1 and out["deleted"] == 3  # batch 1 committed before batch 2 failed
    assert db.rollbacks == 1
    assert any("[trade_tick_retention] prune failed" in r.getMessage() for r in caplog.records)


# ── DB-backed: the real DELETE by pk range on chili_test ─────────────────────
def _ensure_tick_table(db: Session) -> None:
    db.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            id BIGSERIAL PRIMARY KEY,
            symbol VARCHAR(16) NOT NULL,
            observed_at TIMESTAMP NOT NULL,
            price DOUBLE PRECISION NOT NULL,
            size DOUBLE PRECISION NOT NULL
        )
    """))
    db.execute(text(f"DELETE FROM {TABLE}"))
    db.commit()


def test_prune_deletes_old_rows_by_pk_range_on_postgres(db: Session) -> None:
    _ensure_tick_table(db)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for i in range(1, 9):  # ids 1..8: 40..33 days old
        db.execute(text(
            f"INSERT INTO {TABLE} (id, symbol, observed_at, price, size) "
            "VALUES (:i, 'OLD', :ts, 1.0, 100)"
        ), {"i": i, "ts": now - timedelta(days=41 - i)})
    for i in range(9, 13):  # ids 9..12: 1 day old
        db.execute(text(
            f"INSERT INTO {TABLE} (id, symbol, observed_at, price, size) "
            "VALUES (:i, 'NEW', :ts, 1.0, 100)"
        ), {"i": i, "ts": now - timedelta(days=1)})
    db.commit()

    first = prune_trade_ticks(db, retention_days=14, batch_ids=3, max_batches=2)
    assert first["ok"], first
    assert first["cutoff_id"] == 9 and first["batches"] == 2 and first["deleted"] == 6
    assert first["exhausted"] is False and first["remaining_ids"] == 2
    left = db.execute(text(f"SELECT id FROM {TABLE} ORDER BY id")).scalars().all()
    assert left == [7, 8, 9, 10, 11, 12]

    second = prune_trade_ticks(db, retention_days=14, batch_ids=3, max_batches=2)
    assert second["ok"] and second["deleted"] == 2 and second["exhausted"] is True
    left = db.execute(text(f"SELECT symbol FROM {TABLE} ORDER BY id")).scalars().all()
    assert left == ["NEW"] * 4


# ── migration 375: per-table autovacuum scale factors ────────────────────────
def test_migration_375_registered_exactly_once_and_number_unused_elsewhere() -> None:
    ids = [vid for vid, _fn in mg.MIGRATIONS]
    assert ids.count("375_append_heavy_autovacuum_scale_factor") == 1
    assert [v for v in ids if v.startswith("375_")] == ["375_append_heavy_autovacuum_scale_factor"]
    mg._assert_migration_ids_unique()


def test_migration_375_sets_scale_factors_on_present_tables_idempotently(db: Session) -> None:
    _ensure_tick_table(db)
    engine = db.get_bind()
    with engine.connect() as conn:
        mg._migration_375_append_heavy_autovacuum_scale_factor(conn)
        mg._migration_375_append_heavy_autovacuum_scale_factor(conn)  # IDEMPOTENT

    names = list(mg._MIG375_APPEND_HEAVY_TABLES)
    present = {
        r[0] for r in db.execute(
            text("SELECT relname FROM pg_class WHERE relkind = 'r' AND relname = ANY(:n)"),
            {"n": names},
        ).fetchall()
    }
    assert TABLE in present  # the tape is the whole point
    rows = db.execute(
        text("SELECT relname, reloptions FROM pg_class WHERE relkind = 'r' AND relname = ANY(:n)"),
        {"n": names},
    ).fetchall()
    for relname, reloptions in rows:
        opts = list(reloptions or [])
        assert "autovacuum_vacuum_scale_factor=0.02" in opts, (relname, opts)
        assert "autovacuum_analyze_scale_factor=0.02" in opts, (relname, opts)


# ── scheduler wiring: mirrors the NBBO prune (role gate, 6h, guarded runner) ──
def _no_exception_logged(caplog) -> bool:
    return not any(
        rec.exc_info or "failed" in rec.getMessage().lower() for rec in caplog.records
    )


def test_job_flag_off_short_circuits_without_touching_the_db(monkeypatch, caplog) -> None:
    monkeypatch.setattr(settings, "chili_momentum_trade_tick_prune_enabled", False, raising=False)
    import app.db as _db_mod

    def _boom():
        raise AssertionError("SessionLocal must not be opened when the flag is off")

    monkeypatch.setattr(_db_mod, "SessionLocal", _boom)
    with caplog.at_level(logging.WARNING, logger="app.services.trading_scheduler"):
        trading_scheduler._run_trade_tick_prune_job()
    assert _no_exception_logged(caplog), [r.getMessage() for r in caplog.records]


def test_job_reaches_prune_with_its_own_session_and_closes_it(monkeypatch, caplog) -> None:
    monkeypatch.setattr(settings, "chili_momentum_trade_tick_prune_enabled", True, raising=False)
    seen: dict[str, object] = {}

    class _Sess:
        closed = False
        rolled_back = False

        def rollback(self):
            type(self).rolled_back = True

        def close(self):
            type(self).closed = True

    import app.db as _db_mod

    monkeypatch.setattr(_db_mod, "SessionLocal", lambda: _Sess())

    def _fake_prune(db, **kw):
        seen["db"] = db
        seen["kw"] = kw
        return {"ok": True, "deleted": 0, "batches": 0}

    monkeypatch.setattr(ttr, "prune_trade_ticks", _fake_prune)
    with caplog.at_level(logging.WARNING, logger="app.services.trading_scheduler"):
        trading_scheduler._run_trade_tick_prune_job()
    assert _no_exception_logged(caplog), [r.getMessage() for r in caplog.records]
    assert isinstance(seen.get("db"), _Sess)
    assert seen.get("kw") == {}  # knobs come from settings inside the prune, like the NBBO job
    assert _Sess.closed and _Sess.rolled_back


def _add_job_calls_by_id(tree: ast.AST) -> dict[str, tuple[ast.Call, list[ast.If]]]:
    """Map add_job id -> (call, enclosing If chain) via an AST walk with parents."""
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    out: dict[str, tuple[ast.Call, list[ast.If]]] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_job"):
            continue
        job_id = next((kw.value.value for kw in node.keywords
                       if kw.arg == "id" and isinstance(kw.value, ast.Constant)), None)
        if job_id is None:
            continue
        chain: list[ast.If] = []
        p = parents.get(node)
        while p is not None:
            if isinstance(p, ast.If):
                chain.append(p)
            p = parents.get(p)
        out[str(job_id)] = (node, chain)
    return out


def test_scheduler_registers_the_tick_prune_beside_the_nbbo_prune() -> None:
    tree = ast.parse(inspect.getsource(trading_scheduler))
    jobs = _add_job_calls_by_id(tree)
    assert "momentum_nbbo_prune" in jobs and "momentum_trade_tick_prune" in jobs

    def _shape(call: ast.Call) -> dict[str, object]:
        kws = {kw.arg: kw.value for kw in call.keywords}
        trig = kws["trigger"]
        assert isinstance(trig, ast.Call) and trig.func.id == "IntervalTrigger"
        trig_kw = {kw.arg: kw.value.value for kw in trig.keywords if isinstance(kw.value, ast.Constant)}
        return {
            "fn": call.args[0].id,
            "trigger": trig_kw,
            "max_instances": kws["max_instances"].value,
            "coalesce": kws["coalesce"].value,
            "replace_existing": kws["replace_existing"].value,
        }

    nbbo, nbbo_ifs = jobs["momentum_nbbo_prune"]
    tick, tick_ifs = jobs["momentum_trade_tick_prune"]
    n, t = _shape(nbbo), _shape(tick)
    assert t["fn"] == "_run_trade_tick_prune_job" and n["fn"] == "_run_nbbo_spread_prune_job"
    # MIRROR: same cadence, same single-instance/coalesce/replace shape
    assert t["trigger"] == n["trigger"] == {"hours": 6}
    assert (t["max_instances"], t["coalesce"], t["replace_existing"]) == \
           (n["max_instances"], n["coalesce"], n["replace_existing"]) == (1, True, True)
    # same ROLE gate (include_data_recording) on the innermost enclosing if
    assert "include_data_recording" in ast.unparse(tick_ifs[0].test)
    assert "include_data_recording" in ast.unparse(nbbo_ifs[0].test)
    assert "chili_momentum_trade_tick_prune_enabled" in ast.unparse(tick_ifs[0].test)


def test_job_wrapper_uses_the_guarded_runner_with_its_own_id() -> None:
    src = inspect.getsource(trading_scheduler._run_trade_tick_prune_job)
    tree = ast.parse(src)
    guarded = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "run_scheduler_job_guarded"
    ]
    assert len(guarded) == 1
    assert guarded[0].args[0].value == "momentum_trade_tick_prune"
