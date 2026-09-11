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
(2026-09-11: 0.090 s / 0.636 s, n=19,999, 28 symbols), derived once per bench, pinned, and
shared by both arms. A source row that already carries real clocks keeps them.

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


def test_post_mirror_invariant_aborts_publication_clock_blind(tape_db):
    readers = {"entry_gates.leg_prints_between": EG.leg_prints_between}
    # (a) the old, clock-less mirror -> blind
    tape_db.execute(text(
        "INSERT INTO iqfeed_trade_ticks (symbol, observed_at, price, size, bid, ask, source) "
        "VALUES ('VEEE', :o, 10, 100, 9.99, 10.01, 'replay_v3')"), {"o": T0})
    with pytest.raises(P.LivePinUnavailable) as exc:
        P.assert_publication_clock_visible(tape_db, "VEEE", readers=readers)
    assert exc.value.code == "publication_clock_blind"
    tape_db.execute(text("DELETE FROM iqfeed_trade_ticks"))
    # (b) stamped -> visible at the first publication instant, nothing before it
    _insert(tape_db, [P.trade_row_with_clocks("VEEE", _hydrated_row(s), PUB) for s in (0.0, 0.5)])
    out = P.assert_publication_clock_visible(tape_db, "VEEE", readers=readers)
    assert out["status"] == "visible"
    assert out["probe_rows"] == 1
    assert out["readers"]["entry_gates.leg_prints_between"]["pre_publication_rows"] == 0
    assert out["first_available_at_utc"] == (T0.replace(tzinfo=timezone.utc)
                                             + timedelta(seconds=0.603)).isoformat()
    # (c) a reader that cannot see the stamped rows -> blind (fail closed, never "ok")
    with pytest.raises(P.LivePinUnavailable) as exc2:
        P.assert_publication_clock_visible(tape_db, "VEEE", readers={"x": lambda *a, **k: []})
    assert exc2.value.code == "publication_clock_blind"
    # (d) a reader that sees a row BEFORE its available_at -> look-ahead
    with pytest.raises(P.LivePinUnavailable) as exc3:
        P.assert_publication_clock_visible(tape_db, "VEEE", readers={"leaky": lambda *a, **k: [1]})
    assert exc3.value.code == "publication_clock_lookahead"


def test_the_driver_probes_with_the_readers_every_tree_has():
    src = _DRIVER.read_text(encoding="utf-8")
    body = src[src.index("def _publication_probe_readers"):src.index("def resolve_live_pins")]
    assert "signed_tape_query" in body          # #1392, present in BOTH arms of the A/B
    assert 'hasattr(_eg_probe, "leg_prints_between")' in body   # #1385, arm B only
    run = src[src.index("def run_arm("):src.index("# Relations the replay itself writes")]
    assert run.index("mirror_nbbo_streaming(eng)") < run.index("assert_publication_clock_visible(")


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

def test_the_tape_purge_is_not_symbol_scoped_and_covers_the_depth_mirror():
    src = _DRIVER.read_text(encoding="utf-8")
    body = src[src.index("def _purge_replay_tape"):src.index("def _freeze_live_notional_ceiling")]
    assert "WHERE source='replay_v3'" in body and "symbol" not in body.split('"""')[-1]
    import replay_v3_fsm_window as drv

    assert set(drv._REPLAY_TAPE_TABLES) == {
        "iqfeed_trade_ticks", "momentum_nbbo_spread_tape", "iqfeed_depth_snapshots"}
    run = src[src.index("def run_arm("):src.index("# Relations the replay itself writes")]
    assert run.count("_purge_replay_tape(db)") == 2            # before the mirror AND at the end
    assert "AND symbol=:s" not in run


def test_the_driver_resolves_pins_before_the_sink_reset():
    src = _DRIVER.read_text(encoding="utf-8")
    main = src[src.index("def main():"):]
    assert main.index("resolve_live_pins()") < main.index("_reset_sim_sink()")
    assert 'os.environ.get("REPLAY_LIVE_PINS")' in src
