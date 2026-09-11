"""[E] (2026-09-11): the replay mirror stamps PUBLICATION CLOCKS, so the lane's own print
readers can see the replayed tape.

THE DEFECT. ``replay_v3_fsm_window.mirror_ticks_streaming`` inserted ``(symbol, observed_at,
price, size, bid, ask, source)`` only. Since #1392 (987e2b2ad) and #1385 (140fd0f08) every
print read requires ``received_at <= :available_by AND available_at <= :available_by``
(``tape_selection.signed_tape_query``; ``entry_gates._VERDICT_AVAILABLE_BOUND`` ->
``leg_prints_between`` / ``leg_prints_since_high``). With both clocks NULL in the sink every
one of those reads returned ZERO rows: the whole-sale G/D verdict and the deadman walk never
decided in any bench, and the tape-gated entry triggers failed closed.

THE FIX. Each mirrored row carries ``received_at = observed_at + recv_lag`` and
``available_at = observed_at + avail_lag`` -- the p50 lags of the LIVE ``iqfeed_l1`` tape
(the binding pin of the [E] A/B, E_live_pins_20260911T1127Z.json: 0.091193 s / 0.572758 s,
n = 19,999, 32 symbols, 11:21:31-11:26:59Z), derived once per bench, pinned, and shared by
both arms. A source row that already carries real clocks keeps them. (The fixtures below use
round numbers of the same order; nothing here depends on the pinned values.)

[E] REVIEW (2026-09-11) adds: the probe must find the first print readable by the LAST grid
tick (a late-clocked source tape probed ``visible`` while the window read nothing); the purge
is tested for behaviour on temp tables; the pin names its sample regime and every run says
whether its window matched it; the fence is read from the lane env the same way by both
derivation paths; every sibling driver builds its rows with the shared stamp.

The DB tests use a session-local TEMP TABLE (the ``tests/test_tape_selection_contract.py``
idiom), under a NON-UTC session TimeZone, so no app table is touched or truncated.

Runnable: pytest tests/test_replay_v3_publication_clock_mirror.py -v
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in (str(_ROOT), str(_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import replay_live_pins as P  # noqa: E402

from app.services.trading.momentum_neural import entry_gates as EG  # noqa: E402
from app.services.trading.momentum_neural.tape_selection import signed_tape_query  # noqa: E402

_DRIVER = _ROOT / "scripts" / "replay_v3_fsm_window.py"

# The pinned binding values of 2026-09-11 (live chili, last 20,000 iqfeed_l1 prints by id).
PUB = {"recv_lag_s": 0.093, "avail_lag_s": 0.603}
T0 = datetime(2026, 7, 13, 13, 0, 0)          # naive UTC, the iqfeed_trade_ticks convention
HYDRATION_WALL = datetime(2026, 9, 3, 6, 7, 18, tzinfo=timezone.utc)


# ─── fixtures ────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def tape_db():
    """A Session on ONE connection whose ``iqfeed_trade_ticks`` is a TEMP TABLE (shadows the
    real one in this session only), under a non-UTC session TimeZone."""
    url = os.environ["TEST_DATABASE_URL"]
    engine = create_engine(url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT current_database()")).scalar().endswith("_test")
        conn.execute(text("SET LOCAL statement_timeout='20s'"))
        conn.execute(text("SET LOCAL TIME ZONE 'Pacific/Honolulu'"))
        conn.execute(text(
            "CREATE TEMP TABLE iqfeed_trade_ticks (id bigserial, symbol text, "
            "observed_at timestamp, price float8, size float8, bid float8, ask float8, "
            "source text, provider_event_at timestamptz, received_at timestamptz, "
            "available_at timestamptz, timestamp_basis text)"))
        with Session(bind=conn) as db:
            yield db
        conn.rollback()
    engine.dispose()


def _insert(db, rows):
    cols = ", ".join(P.TRADE_MIRROR_INSERT_COLUMNS)
    marks = ", ".join(f":c{i}" for i in range(len(P.TRADE_MIRROR_INSERT_COLUMNS)))
    for r in rows:
        db.execute(text(f"INSERT INTO iqfeed_trade_ticks ({cols}) VALUES ({marks})"),
                   {f"c{i}": v for i, v in enumerate(r)})


def _hydrated_row(seconds, price=10.0):
    """The hydrated source shape the mirror reads: received_at = the hydration wall time,
    available_at NULL (VEEE 2026-07-13, chili_hydrated, measured 2026-09-11)."""
    return (T0 + timedelta(seconds=seconds), price, 100.0, price - .01, price + .01, 1,
            HYDRATION_WALL, None, T0 + timedelta(seconds=seconds), None)


# ─── 1. the stamp ────────────────────────────────────────────────────────────────────────

def test_stamped_clocks_are_observed_plus_derived_lags_and_ordered():
    obs = T0 + timedelta(seconds=12.25)
    pev, recv, avail, basis, kind = P.publication_clocks(
        obs, PUB, source_received_at=HYDRATION_WALL, source_available_at=None)
    aware = obs.replace(tzinfo=timezone.utc)
    assert kind == "derived" and basis == P.PUBLICATION_TIMESTAMP_BASIS
    assert pev == aware
    assert recv == aware + timedelta(seconds=0.093)
    assert avail == aware + timedelta(seconds=0.603)
    assert aware <= recv <= avail
    assert recv.tzinfo is not None and avail.tzinfo is not None
    # the hydration wall time is NOT a publication clock and never leaks into the stamp
    assert recv < HYDRATION_WALL


def test_a_source_row_with_real_clocks_keeps_them_even_900s_late():
    """A LIVE-tape source (iqfeed_l1) already carries its publication clocks; a 15-min
    delayed-entitlement row was seen 900 s late by live too, so parity keeps it."""
    obs = T0
    recv = obs.replace(tzinfo=timezone.utc) + timedelta(seconds=900.2)
    avail = recv + timedelta(seconds=0.4)
    pev, r, a, basis, kind = P.publication_clocks(
        obs, PUB, source_received_at=recv, source_available_at=avail,
        source_provider_event_at=obs, source_timestamp_basis="iqfeed_q_bid_ask_time_clock")
    assert (r, a, kind, basis) == (recv, avail, "source", "iqfeed_q_bid_ask_time_clock")
    # reversed source clocks are not "real": derived instead
    *_x, kind2 = P.publication_clocks(obs, PUB, source_received_at=avail, source_available_at=recv)
    assert kind2 == "derived"


def test_the_mirror_row_matches_the_insert_columns_and_counts_its_kind():
    counts: dict = {}
    row = P.trade_row_with_clocks("VEEE", _hydrated_row(1.5), PUB, clock_counts=counts)
    assert len(row) == len(P.TRADE_MIRROR_INSERT_COLUMNS)
    rec = dict(zip(P.TRADE_MIRROR_INSERT_COLUMNS, row))
    assert rec["source"] == "replay_v3" and rec["symbol"] == "VEEE"
    assert rec["observed_at"].tzinfo is None                       # naive UTC column
    assert rec["available_at"] - rec["observed_at"].replace(tzinfo=timezone.utc) == timedelta(seconds=0.603)
    assert counts == {"derived": 1}
    # the in-memory fallback mirror passes only (observed, price, size, bid, ask, id)
    short = P.trade_row_with_clocks("VEEE", _hydrated_row(2)[:6], PUB, clock_counts=counts)
    assert dict(zip(P.TRADE_MIRROR_INSERT_COLUMNS, short))["timestamp_basis"] == P.PUBLICATION_TIMESTAMP_BASIS
    assert counts == {"derived": 2}


def test_both_driver_mirrors_write_the_clock_columns():
    import ast

    src = _DRIVER.read_text(encoding="utf-8")
    tree = ast.parse(src)
    bodies = {n.name: ast.get_source_segment(src, n) for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef)}
    for fn in ("mirror_ticks_streaming", "mirror_ticks"):
        assert "TRADE_MIRROR_INSERT_COLUMNS" in bodies[fn], fn
        assert "trade_row_with_clocks(" in bodies[fn], fn
        assert "'replay_v3')" not in bodies[fn], f"{fn} still builds the clock-less row"
    import replay_v3_fsm_window as drv

    sql = drv.trade_mirror_sql(())
    for col in ("received_at", "available_at", "provider_event_at", "timestamp_basis"):
        assert col in sql, col
    assert sql.rstrip().endswith("ORDER BY observed_at ASC, id ASC")


# ─── 2. the readers, against real PostgreSQL ─────────────────────────────────────────────

def test_leg_prints_between_reads_mirrored_rows_only_after_their_available_at(tape_db):
    """No look-ahead: a print is invisible until observed + avail_lag, visible from then."""
    _insert(tape_db, [P.trade_row_with_clocks("VEEE", _hydrated_row(s, 10 + s), PUB)
                      for s in (0.0, 1.0, 2.0)])
    first_obs = T0
    before = first_obs + timedelta(seconds=0.603) - timedelta(microseconds=1)
    at = first_obs + timedelta(seconds=0.603)
    after = first_obs - timedelta(seconds=1)
    assert EG.leg_prints_between("VEEE", db=tape_db, after=after, as_of=before) == []
    rows = EG.leg_prints_between("VEEE", db=tape_db, after=after, as_of=at)
    assert [float(r[0]) for r in rows] == [10.0]
    rows3 = EG.leg_prints_between("VEEE", db=tape_db, after=after, as_of=T0 + timedelta(seconds=3))
    assert [float(r[0]) for r in rows3] == [10.0, 11.0, 12.0]
    # the entry window read (#1392) sees exactly the same population
    q, p = signed_tape_query("VEEE", as_of=T0 + timedelta(seconds=1.7), window_prints=None, window_s=5)
    assert [float(r[0]) for r in tape_db.execute(text(q), p)] == [10.0, 11.0]


def test_null_clock_rows_are_invisible_to_verdict_and_count_reads(tape_db):
    """PINS THE OLD BLINDNESS: the pre-[E] mirror row shape reads as NOTHING everywhere."""
    for s in (0.0, 1.0, 2.0):
        tape_db.execute(text(
            "INSERT INTO iqfeed_trade_ticks (symbol, observed_at, price, size, bid, ask, source) "
            "VALUES ('VEEE', :o, 10, 100, 9.99, 10.01, 'replay_v3')"),
            {"o": T0 + timedelta(seconds=s)})
    late = T0 + timedelta(minutes=5)
    assert EG.leg_prints_between("VEEE", db=tape_db, after=T0 - timedelta(seconds=1), as_of=late) == []
    assert EG.leg_prints_since_high("VEEE", db=tape_db, hi_at=T0 - timedelta(seconds=1), hi_id=0,
                                    as_of=late) == []
    assert EG.signed_tape_accel_features("VEEE", db=tape_db, as_of=late, window_prints=255) is None


WIN_END = T0 + timedelta(minutes=60)     # the last replay read instant in these fixtures


def test_post_mirror_invariant_aborts_publication_clock_blind(tape_db):
    readers = {"entry_gates.leg_prints_between": EG.leg_prints_between}
    # (a) the old, clock-less mirror -> blind
    tape_db.execute(text(
        "INSERT INTO iqfeed_trade_ticks (symbol, observed_at, price, size, bid, ask, source) "
        "VALUES ('VEEE', :o, 10, 100, 9.99, 10.01, 'replay_v3')"), {"o": T0})
    with pytest.raises(P.LivePinUnavailable) as exc:
        P.assert_publication_clock_visible(tape_db, "VEEE", readers=readers, visible_by=WIN_END)
    assert exc.value.code == "publication_clock_blind"
    tape_db.execute(text("DELETE FROM iqfeed_trade_ticks"))
    # (b) stamped -> visible at the first publication instant, nothing before it
    _insert(tape_db, [P.trade_row_with_clocks("VEEE", _hydrated_row(s), PUB) for s in (0.0, 0.5)])
    out = P.assert_publication_clock_visible(tape_db, "VEEE", readers=readers, visible_by=WIN_END)
    assert out["status"] == "visible"
    assert out["probe_rows"] == 1
    assert out["readers"]["entry_gates.leg_prints_between"]["pre_publication_rows"] == 0
    assert out["first_available_at_utc"] == (T0.replace(tzinfo=timezone.utc)
                                             + timedelta(seconds=0.603)).isoformat()
    assert out["readable_margin_s"] == pytest.approx(3600.0 - 0.603)
    # (c) a reader that cannot see the stamped rows -> blind (fail closed, never "ok")
    with pytest.raises(P.LivePinUnavailable) as exc2:
        P.assert_publication_clock_visible(tape_db, "VEEE", readers={"x": lambda *a, **k: []},
                                           visible_by=WIN_END)
    assert exc2.value.code == "publication_clock_blind"
    # (d) a reader that sees a row BEFORE its available_at -> look-ahead
    with pytest.raises(P.LivePinUnavailable) as exc3:
        P.assert_publication_clock_visible(tape_db, "VEEE", readers={"leaky": lambda *a, **k: [1]},
                                           visible_by=WIN_END)
    assert exc3.value.code == "publication_clock_lookahead"


def test_a_source_tape_published_after_the_window_is_blind_not_visible(tape_db):
    """[E] review: the probe proved visibility at the print's OWN available_at, never inside
    the replay. A source tape with real but LATE clocks -- a backfill whose received_at /
    available_at were written at insert time, after WIN_END -- keeps those clocks
    (publication_clocks: known and ordered), so every clock-bounded read of the sim window is
    empty. It used to probe 'visible'. It is blind."""
    readers = {"entry_gates.leg_prints_between": EG.leg_prints_between}
    backfilled_at = (WIN_END + timedelta(hours=2)).replace(tzinfo=timezone.utc)
    rows = []
    for s in (0.0, 1.0, 2.0):
        src = (T0 + timedelta(seconds=s), 10.0 + s, 100.0, 9.99, 10.01, 1,
               backfilled_at, backfilled_at + timedelta(seconds=0.2), None, "backfill_insert_time")
        rows.append(P.trade_row_with_clocks("VEEE", src, PUB))
    _insert(tape_db, rows)
    # the reader DOES see the rows at their own (late) publication instant ...
    assert EG.leg_prints_between("VEEE", db=tape_db, after=T0 - timedelta(seconds=1),
                                 as_of=backfilled_at.replace(tzinfo=None) + timedelta(seconds=1))
    # ... and sees NOTHING at the window's last read: the run would score silence
    assert EG.leg_prints_between("VEEE", db=tape_db, after=T0 - timedelta(seconds=1),
                                 as_of=WIN_END) == []
    with pytest.raises(P.LivePinUnavailable) as exc:
        P.assert_publication_clock_visible(tape_db, "VEEE", readers=readers, visible_by=WIN_END)
    assert exc.value.code == "publication_clock_blind"
    assert "after the replay's last read" in exc.value.detail
    # the same late tape probed against a read instant AFTER its publication is visible
    later = backfilled_at + timedelta(minutes=1)
    assert P.assert_publication_clock_visible(
        tape_db, "VEEE", readers=readers, visible_by=later)["status"] == "visible"


def test_the_probe_needs_a_last_read_instant():
    with pytest.raises(TypeError):
        P.assert_publication_clock_visible(object(), "VEEE", readers={"x": lambda *a, **k: [1]})


def test_the_lane_print_readers_are_the_lanes_own():
    """One definition for every driver: the entry window read (#1392, every tree) and the
    verdict / deadman batch read (#1385) where the tree has it."""
    readers = P.lane_print_readers()
    assert set(readers) == {"tape_selection.signed_tape_query", "entry_gates.leg_prints_between"}
    assert readers["entry_gates.leg_prints_between"] is EG.leg_prints_between


def _call_keywords(src: str, func: str, callee: str) -> list[set]:
    """The keyword names of every ``callee(...)`` call inside ``func`` (AST, not text)."""
    import ast

    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == func)
    out = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            name = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", None)
            if name == callee:
                out.append({k.arg for k in n.keywords})
    return out


def test_every_driver_probes_the_mirror_by_its_last_grid_tick():
    """Every tick-mirroring driver calls the probe WITH ``visible_by`` (the fail-closed
    signature makes it a TypeError otherwise -- test_the_probe_needs_a_last_read_instant)."""
    root = _ROOT / "scripts"
    for script, func in (("replay_v3_fsm_window.py", "run_arm"),
                         ("replay_ab_dark_flags.py", "run_arm"),
                         ("replay_window.py", "run_arm"),
                         ("replay_v3_upc_0629.py", "probe_mirror")):
        calls = _call_keywords((root / script).read_text(encoding="utf-8"), func,
                               "assert_publication_clock_visible")
        assert calls and all("visible_by" in kw and "readers" in kw for kw in calls), script


# ─── 3. the derivation ───────────────────────────────────────────────────────────────────

def _lag_rows(n_rt, n_delayed, *, avail=0.6, recv=0.09):
    rows = []
    for i in range(n_rt):
        rows.append((f"S{i % 7}", T0 + timedelta(seconds=i), recv + (i % 5) * 0.001, avail + (i % 11) * 0.01))
    for i in range(n_delayed):
        rows.append(("DLY", T0 + timedelta(seconds=i), 900.1, 900.3))
    return rows


def test_derive_publication_lag_excludes_delayed_rows_and_fails_closed_below_n_min():
    out = P.summarize_publication_lags(_lag_rows(150, 50), fence_s=300.0)
    assert out["n"] == 150 and out["excluded_delayed"] == 50 and out["rows_read"] == 200
    assert out["n_symbols"] == 7 and "DLY" not in out["per_symbol"]
    assert out["avail_lag_s"] == pytest.approx(0.65, abs=0.011)
    assert out["recv_lag_s"] == pytest.approx(0.092, abs=0.0011)
    assert out["avail_lag_s"] >= out["recv_lag_s"]
    lo, hi = out["available_lag_s"]["p50_ci95"]
    assert lo <= out["avail_lag_s"] <= hi
    assert P.PUBLICATION_LAG_N_MIN == 100
    with pytest.raises(P.LivePinUnavailable) as exc:
        P.summarize_publication_lags(_lag_rows(99, 5000), fence_s=300.0)
    assert exc.value.code == "publication_clock_unavailable"


def test_the_derivation_sql_runs_the_readers_predicate(tape_db):
    """The live SQL itself, on the temp tables: NULL / reversed clocks and other sources are
    not in the sample; delayed rows are counted and excluded; the multiplier is the newest
    LIVE broker-truth receipt of the family."""
    conn = tape_db.connection()
    conn.execute(text(
        "CREATE TEMP TABLE trading_automation_sessions (id bigint, symbol text, mode text, "
        "execution_family text, updated_at timestamp, risk_snapshot_json jsonb)"))
    base = T0.replace(tzinfo=timezone.utc)
    for i in range(130):
        o = base + timedelta(seconds=i)
        conn.execute(text(
            "INSERT INTO iqfeed_trade_ticks (symbol, observed_at, price, size, source, "
            "received_at, available_at) VALUES (:s, :o, 1, 1, 'iqfeed_l1', :r, :a)"),
            {"s": f"S{i % 3}", "o": o.replace(tzinfo=None),
             "r": o + timedelta(seconds=0.1), "a": o + timedelta(seconds=0.6)})
    for i in range(4):   # delayed entitlement
        o = base + timedelta(seconds=i)
        conn.execute(text(
            "INSERT INTO iqfeed_trade_ticks (symbol, observed_at, price, size, source, "
            "received_at, available_at) VALUES ('DLY', :o, 1, 1, 'iqfeed_l1', :r, :a)"),
            {"o": o.replace(tzinfo=None), "r": o + timedelta(seconds=900), "a": o + timedelta(seconds=900.5)})
    for r_, a_ in ((None, base), (base, None), (base + timedelta(seconds=2), base)):  # ineligible
        conn.execute(text(
            "INSERT INTO iqfeed_trade_ticks (symbol, observed_at, price, size, source, "
            "received_at, available_at) VALUES ('BAD', :o, 1, 1, 'iqfeed_l1', :r, :a)"),
            {"o": T0, "r": r_, "a": a_})
    conn.execute(text(
        "INSERT INTO iqfeed_trade_ticks (symbol, observed_at, price, size, source) "
        "VALUES ('HYD', :o, 1, 1, 'iqfeed_lookup_hist')"), {"o": T0})
    for sid, mode, src, mult in ((10, "live", "broker_multiplier", 4.0),
                                 (11, "paper", "broker_multiplier", 2.0),
                                 (12, "live", "assume_cash", 1.0)):
        conn.execute(text(
            "INSERT INTO trading_automation_sessions VALUES (:id, 'SXTC', :m, 'alpaca_spot', :u, "
            "CAST(:j AS jsonb))"),
            {"id": sid, "m": mode, "u": T0, "j": json.dumps({"momentum_policy_caps_derivation": {
                "notional_ceiling": {"source": src, "multiplier": mult, "equity_usd": 10299.27}}})})

    raw = conn.connection.dbapi_connection

    class _Shared:
        def set_session(self, **_k):
            raise RuntimeError("inside a transaction")   # derive_live_pins tolerates this

        def cursor(self):
            return raw.cursor()

        def rollback(self):
            pass                                           # keep the temp tables for the test

        def close(self):
            pass

    pins = P.derive_live_pins("postgresql://x@h/chili", execution_family="alpaca_spot",
                              connect=lambda _dsn: _Shared(), n_min=100)
    pub = pins["publication_clock"]
    assert pub["n"] == 130 and pub["excluded_delayed"] == 4 and pub["n_symbols"] == 3
    assert pub["recv_lag_s"] == pytest.approx(0.1, abs=1e-6)
    assert pub["avail_lag_s"] == pytest.approx(0.6, abs=1e-6)
    mult = pins["broker_multiplier"]
    assert (mult["multiplier"], mult["source"], mult["session_id"]) == (4.0, "broker_multiplier", 10)
    assert pins["schema"] == P.LIVE_PINS_SCHEMA and pins["source_db"] == "chili"
    # round trip through the env value the bench sends
    again = P.load_live_pins(P.dumps_live_pins(pins))
    assert P.pins_sha256(again) == P.pins_sha256(pins)


# ─── 4. the named constants are the repo's own ───────────────────────────────────────────

def test_the_real_time_fence_is_the_frontier_probe_setting():
    from app.config import Settings

    field = Settings.model_fields["chili_momentum_halt_frontier_max_arrival_delay_s"]
    assert float(field.default) == P.REALTIME_ARRIVAL_FENCE_S_DEFAULT
    assert P.REALTIME_ARRIVAL_FENCE_SOURCE.endswith("chili_momentum_halt_frontier_max_arrival_delay_s")
    tail = Settings.model_fields["chili_momentum_halt_frontier_tail_rows"]
    le = [m.le for m in tail.metadata if getattr(m, "le", None) is not None]
    assert le and int(le[0]) == P.PUBLICATION_LAG_SAMPLE_ROWS


def test_n_min_rule_matches_held_bbo():
    from app.services.trading.momentum_neural.held_bbo import n_min_for_percentile

    for p in (0.5, 0.9, 0.99, 0.999):
        assert P.n_min_for_percentile(p) == n_min_for_percentile(p)


def test_validate_refuses_a_pin_without_both_lags():
    good = {"schema": P.LIVE_PINS_SCHEMA, "publication_clock": dict(PUB), "broker_multiplier": {}}
    assert P.validate_live_pins(good) is good
    for bad in ({**good, "schema": "v0"},
                {**good, "publication_clock": {"recv_lag_s": 0.1}},
                {**good, "publication_clock": {"recv_lag_s": 0.7, "avail_lag_s": 0.6}},
                {**good, "broker_multiplier": None}):
        with pytest.raises(P.LivePinUnavailable):
            P.validate_live_pins(bad)


# ─── 5. the sink starts empty of every earlier window's tape ─────────────────────────────

def test_the_tape_purge_removes_every_replay_row_of_every_symbol_and_keeps_the_rest(tape_db):
    """BEHAVIOUR, on temp tables ([E] review: the old test matched source text). A killed
    driver never runs its end-of-arm delete, so the sink carries OTHER windows' tape; the
    purge must take every replay_v3 row of all three tape tables -- whatever the symbol --
    and nothing else, and say how many there were."""
    import replay_v3_fsm_window as drv

    tape_db.execute(text("CREATE TEMP TABLE momentum_nbbo_spread_tape "
                         "(id bigserial, symbol text, observed_at timestamp, source text)"))
    tape_db.execute(text("CREATE TEMP TABLE iqfeed_depth_snapshots "
                         "(id bigserial, symbol text, observed_at timestamp, source text)"))
    residue = {"iqfeed_trade_ticks": 0, "momentum_nbbo_spread_tape": 0, "iqfeed_depth_snapshots": 0}
    for tbl in residue:
        for sym, src, n in (("HYFM", "replay_v3", 3), ("MIMI", "replay_v3", 2),
                            ("VEEE", "replay_v3", 1), ("VEEE", "iqfeed_l1", 4)):
            for i in range(n):
                tape_db.execute(text(f"INSERT INTO {tbl} (symbol, observed_at, source) "
                                     "VALUES (:s, :o, :src)"),
                                {"s": sym, "o": T0 + timedelta(seconds=i), "src": src})
            if src == "replay_v3":
                residue[tbl] += n
    out = drv._purge_replay_tape(tape_db)
    assert out == residue == {"iqfeed_trade_ticks": 6, "momentum_nbbo_spread_tape": 6,
                              "iqfeed_depth_snapshots": 6}
    for tbl in residue:
        left = tape_db.execute(text(f"SELECT symbol, source, count(*) FROM {tbl} "
                                    "GROUP BY symbol, source")).fetchall()
        assert [tuple(r) for r in left] == [("VEEE", "iqfeed_l1", 4)], tbl
    # and run_arm purges before the mirror AND at the end (AST: two calls, no symbol arg)
    import ast

    src = _DRIVER.read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "run_arm")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "_purge_replay_tape"]
    assert len(calls) == 2 and all(len(c.args) == 1 and not c.keywords for c in calls)


def test_the_driver_resolves_pins_before_the_sink_reset():
    src = _DRIVER.read_text(encoding="utf-8")
    main = src[src.index("def main():"):]
    assert main.index("resolve_live_pins()") < main.index("_reset_sim_sink()")
    assert 'os.environ.get("REPLAY_LIVE_PINS")' in src


# ─── 6. [E] review: the regime the constant came from ────────────────────────────────────

def test_the_pin_names_its_sample_regime_and_each_run_says_if_its_window_matched():
    """The A/B pin was a 5.5-min PREMARKET sample (11:21:31-11:26:59Z) stamped on RTH-open
    windows; the receipt never said so. ET wall clock, DST-correct (EDT in July)."""
    assert P.session_phases_between("2026-09-11T11:21:31", "2026-09-11T11:26:59") == ["premarket"]
    assert P.session_phases_between("2026-07-13T12:37:00", "2026-07-13T13:37:00") == [
        "premarket", "regular"]
    # EST (UTC-5) in January: 09:30 ET = 14:30Z, so the same UTC hour is a different phase
    assert P.session_phases_between("2026-01-15T15:00:00", "2026-01-15T16:00:00") == ["regular"]
    assert P.session_phases_between("2026-01-15T14:00:00", "2026-01-15T15:00:00") == [
        "premarket", "regular"]
    assert P.session_phases_between("2026-07-15T14:00:00", "2026-07-15T15:00:00") == ["regular"]
    rows = [("TNON", datetime(2026, 9, 11, 11, 21, 31) + timedelta(seconds=i), 0.09, 0.57)
            for i in range(120)]
    pub = P.summarize_publication_lags(rows, fence_s=300.0)
    assert pub["sample_regime"] == {"phases": ["premarket"], "span_s": 119.0}
    assert "NOT conditioned" in pub["caveat"]
    veee = P.regime_match(pub, "2026-07-13T12:37:00", "2026-07-13T13:37:00")
    assert veee == {"pin_phases": ["premarket"], "window_phases": ["premarket", "regular"],
                    "match": False}
    assert P.regime_match(pub, "2026-07-13T11:00:00", "2026-07-13T12:00:00")["match"] is True
    # an older pin without the block still reports (from its observed span)
    old = {k: v for k, v in pub.items() if k != "sample_regime"}
    assert P.regime_match(old, "2026-07-13T12:37:00", "2026-07-13T13:37:00")["pin_phases"] == [
        "premarket"]


def test_the_driver_receipt_carries_the_regime_verdict():
    calls = _call_keywords(_DRIVER.read_text(encoding="utf-8"), "run_arm", "regime_match")
    assert calls, "run_arm must report regime_match(...) in its publication_clock block"


# ─── 7. [E] review: one fence read for both derivation paths ─────────────────────────────

def test_the_fence_comes_from_the_lane_env_the_same_way_on_both_paths(tmp_path, monkeypatch):
    assert P.fence_from_lane_env(None) == (300.0, P.REALTIME_ARRIVAL_FENCE_SOURCE)
    assert P.fence_from_lane_env({"CHILI_MOMENTUM_HALT_FRONTIER_MAX_ARRIVAL_DELAY_S": "120"}) == (
        120.0, "lane_env:CHILI_MOMENTUM_HALT_FRONTIER_MAX_ARRIVAL_DELAY_S")
    for bad in ("x", "-5", "nan"):
        with pytest.raises(P.LivePinUnavailable):
            P.fence_from_lane_env({"CHILI_MOMENTUM_HALT_FRONTIER_MAX_ARRIVAL_DELAY_S": bad})
    # the CLI: --lane-env reads the lane's override (it used to promise this and ignore it)
    env_file = tmp_path / "lane.env"
    env_file.write_text("CHILI_MOMENTUM_HALT_FRONTIER_MAX_ARRIVAL_DELAY_S=150\n"
                        "ALPACA_SECRET=never-read\n", encoding="utf-8")
    seen = {}

    def _derive(dsn, *, execution_family, fence_s, fence_source, **_k):
        seen.update(fence_s=fence_s, fence_source=fence_source, ef=execution_family)
        return {"schema": P.LIVE_PINS_SCHEMA,
                "publication_clock": {"recv_lag_s": 0.09, "avail_lag_s": 0.57, "n": 200,
                                      "n_symbols": 3, "excluded_delayed": 0,
                                      "observed_span_utc": None,
                                      "sample_regime": {"phases": []}},
                "broker_multiplier": {"multiplier": 4.0, "source": "broker_multiplier",
                                      "execution_family": execution_family}}

    monkeypatch.setattr(P, "derive_live_pins", _derive)
    out = tmp_path / "pins.json"
    assert P.main(["--dsn", "postgresql://x@h/chili", "--execution-family", "alpaca_spot",
                   "--out", str(out), "--lane-env", str(env_file)]) == 0
    assert seen == {"fence_s": 150.0, "ef": "alpaca_spot",
                    "fence_source": "lane_env:CHILI_MOMENTUM_HALT_FRONTIER_MAX_ARRIVAL_DELAY_S"}
    assert P.main(["--dsn", "postgresql://x@h/chili", "--execution-family", "alpaca_spot",
                   "--out", str(out), "--fence-s", "90"]) == 0
    assert (seen["fence_s"], seen["fence_source"]) == (90.0, "cli:--fence-s")
    assert P.main(["--dsn", "postgresql://x@h/chili", "--execution-family", "alpaca_spot",
                   "--out", str(out)]) == 0
    assert (seen["fence_s"], seen["fence_source"]) == (300.0, P.REALTIME_ARRIVAL_FENCE_SOURCE)


# ─── 8. [E] review: a pin serves ONE execution family ────────────────────────────────────

def _pins(family="alpaca_spot", mult=4.0):
    return {"schema": P.LIVE_PINS_SCHEMA, "publication_clock": dict(PUB),
            "broker_multiplier": {"multiplier": mult, "source": "broker_multiplier",
                                  "execution_family": family}}


def test_a_pinned_multiplier_is_refused_on_another_family():
    """An alpaca_spot 4.0 reused on a robinhood_agentic_mcp bench served 4.0 to a cash
    account under a '..._pinned' label. Refused by name; case/space-insensitive like the
    app's normalize_execution_family."""
    prov = P.equity_provider_from_pins(13_000.0, _pins(), execution_family=" Alpaca_Spot ")
    assert (prov.replay_multiplier, prov.replay_multiplier_execution_family) == (4.0, "alpaca_spot")
    for other in ("robinhood_agentic_mcp", "robinhood_spot", "coinbase_spot", ""):
        with pytest.raises(P.LivePinUnavailable) as exc:
            P.equity_provider_from_pins(13_000.0, _pins(), execution_family=other)
        assert exc.value.code == "live_pins_family_mismatch"
    legacy = _pins()
    del legacy["broker_multiplier"]["execution_family"]        # a pin that cannot say
    with pytest.raises(P.LivePinUnavailable):
        P.check_pins_family(legacy, "alpaca_spot")


def test_the_app_free_respellings_equal_the_app():
    from app.services.trading.execution_family_registry import normalize_execution_family
    from app.services.trading.momentum_neural import risk_policy as rp

    for v in ("alpaca_spot", " ALPACA_SHORT ", "robinhood_agentic_mcp", "", None):
        assert P.normalize_family(v) == normalize_execution_family(v)
    assert P.REPLAY_EQUITY_SEAM_SOURCE_PREFIX == rp.REPLAY_EQUITY_SEAM_SOURCE
    assert P.BROKER_TRUTH_MULTIPLIER_SOURCES == rp.BROKER_TRUTH_MULTIPLIER_SOURCES


def test_the_app_and_the_bench_pick_the_same_live_multiplier_receipt(tape_db):
    """risk_policy.live_broker_multiplier_receipt (the counterfactual replay's pin) and
    replay_live_pins.BROKER_MULTIPLIER_SQL (the bench's) read the same newest LIVE
    broker-truth receipt -- two replay instruments, one account."""
    from app.services.trading.momentum_neural import risk_policy as rp

    conn = tape_db.connection()
    conn.execute(text(
        "CREATE TEMP TABLE trading_automation_sessions (id bigint, symbol text, mode text, "
        "execution_family text, updated_at timestamp, risk_snapshot_json jsonb)"))
    for sid, mode, ef, src, mult in ((10, "live", "alpaca_spot", "broker_multiplier", 4.0),
                                     (11, "paper", "alpaca_spot", "broker_multiplier", 2.0),
                                     (12, "live", "alpaca_spot", "assume_cash", 1.0),
                                     (13, "live", "robinhood_agentic_mcp", "assume_cash", 1.0)):
        conn.execute(text(
            "INSERT INTO trading_automation_sessions VALUES (:id, 'SXTC', :m, :ef, :u, "
            "CAST(:j AS jsonb))"),
            {"id": sid, "m": mode, "ef": ef, "u": T0, "j": json.dumps(
                {"momentum_policy_caps_derivation": {"notional_ceiling": {
                    "source": src, "multiplier": mult}}})})
    app_pick = rp.live_broker_multiplier_receipt(tape_db)
    assert (app_pick["session_id"], app_pick["multiplier"], app_pick["execution_family"]) == (
        10, 4.0, "alpaca_spot")
    assert rp.live_broker_multiplier_receipt(tape_db, "alpaca_spot")["session_id"] == 10
    assert rp.live_broker_multiplier_receipt(tape_db, "robinhood_agentic_mcp") is None
    raw = conn.connection.dbapi_connection
    cur = raw.cursor()
    cur.execute(P.BROKER_MULTIPLIER_SQL, {"ef": "alpaca_spot",
                                          "sources": list(P.BROKER_TRUTH_MULTIPLIER_SOURCES)})
    bench_pick = P.summarize_broker_multiplier(cur.fetchone(), execution_family="alpaca_spot")
    cur.close()
    assert (bench_pick["session_id"], bench_pick["multiplier"]) == (10, 4.0)


def test_an_unreadable_receipt_table_never_poisons_the_callers_transaction(tape_db):
    """The counterfactual replay reads the receipt mid-run; a failing read must answer None
    and leave the caller's transaction usable (SAVEPOINT)."""
    from app.services.trading.momentum_neural import risk_policy as rp

    tape_db.connection().execute(text(
        "CREATE TEMP TABLE trading_automation_sessions (id bigint, symbol text)"))  # no columns
    assert rp.live_broker_multiplier_receipt(tape_db) is None
    assert tape_db.execute(text("SELECT 1")).scalar() == 1


# ─── 9. [E] review: every driver stamps through the shared helpers ───────────────────────

def test_the_shared_stamp_and_insert_make_rows_the_readers_see(tape_db):
    """stamped_trade_rows + trade_mirror_insert_sql -- what every sibling driver now writes
    with -- produce rows leg_prints_between reads from their available_at, including from a
    pandas frame (NaT -> NULL -> derived; Timestamp -> datetime)."""
    import pandas as pd

    frame = pd.DataFrame({
        "observed_at": [T0, T0 + timedelta(seconds=1)],
        "price": [10.0, 10.5], "size": [100.0, 200.0], "bid": [9.99, 10.49], "ask": [10.01, 10.51],
        "received_at": [pd.NaT, pd.NaT], "available_at": [pd.NaT, pd.NaT],
    })
    src = [(r["observed_at"].to_pydatetime(), r["price"], r["size"], r["bid"], r["ask"], None,
            r["received_at"], r["available_at"], None, None) for _, r in frame.iterrows()]
    counts: dict = {}
    rows = P.stamped_trade_rows("VEEE", src, PUB, clock_counts=counts)
    assert counts == {"derived": 2}
    raw = tape_db.connection().connection.dbapi_connection
    cur = raw.cursor()
    cur.executemany(P.trade_mirror_insert_sql(), rows)
    cur.close()
    after = T0 - timedelta(seconds=1)
    assert EG.leg_prints_between("VEEE", db=tape_db, after=after,
                                 as_of=T0 + timedelta(seconds=0.6)) == []
    got = EG.leg_prints_between("VEEE", db=tape_db, after=after, as_of=T0 + timedelta(seconds=2))
    assert [float(r[0]) for r in got] == [10.0, 10.5]


def test_no_replay_driver_writes_a_hand_written_tick_column_list():
    """Regression fence for the next driver: every ``INSERT INTO iqfeed_trade_ticks`` in a
    replay script takes its columns from TRADE_MIRROR_INSERT_COLUMNS /
    trade_mirror_insert_sql() -- a hand-written (symbol, observed_at, price, size, bid, ask,
    source) list is the clock-less mirror this review found in 3 of 4 drivers."""
    import ast

    offenders = []
    for path in sorted((_ROOT / "scripts").glob("replay*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and "INSERT INTO iqfeed_trade_ticks" in node.value:
                tail = node.value.split("INSERT INTO iqfeed_trade_ticks", 1)[1].strip()
                if tail not in ("(", ""):
                    offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], offenders
    for script in ("replay_ab_dark_flags.py", "replay_window.py", "replay_v3_upc_0629.py"):
        src = (_ROOT / "scripts" / script).read_text(encoding="utf-8")
        assert "stamped_trade_rows(" in src, script


def test_the_golden_child_needs_the_pin_before_any_app_import():
    """The golden driver refuses to start without REPLAY_LIVE_PINS -- checked right after
    its build authority, before the app imports (static order: the child cannot be run here
    without a clean-build authority)."""
    import ast

    src = (_ROOT / "scripts" / "replay_ab_dark_flags.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    body = tree.body
    idx_pin = next(i for i, n in enumerate(body) if isinstance(n, ast.Assign) and any(
        getattr(t, "id", None) == "_LIVE_PINS_RAW" for t in n.targets))
    idx_app = next(i for i, n in enumerate(body) if (
        isinstance(n, ast.ImportFrom) and (n.module or "").startswith("app.")) or (
        isinstance(n, ast.Import) and any(a.name.startswith("app.") for a in n.names)))
    idx_auth = next(i for i, n in enumerate(body) if isinstance(n, ast.Assign) and any(
        getattr(t, "id", None) == "_actual_build_sha" for t in n.targets))
    assert idx_auth < idx_pin < idx_app
