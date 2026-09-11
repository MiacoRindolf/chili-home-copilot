"""[5] THE TOPPING TAIL READS THE LEG'S OWN PRINTS, NOT A 15-MINUTE BUCKET (2026-09-11).

The planner row said "0 fires even with a frame". That premise is stale: since e91c18092 (the
15m fallback frame, 2026-09-07) the runner site fired 3 times live -- WYHG 09-08 09:09:04
(+$19.60), WYHG 09:10:12 (+$5.64), VIOT 09-10 17:07:41 (-$7.08). The real defect is the
INPUT. The 15-minute wall-clock bucket includes prints from BEFORE the position existed:

    WYHG 09:09:04  bucket 6.06/6.36/5.78/5.9294  high 6.36 printed 09:03:35, entry fill
                   09:08:37 -> upper wick 51.7% = topping tail
                   leg    5.89/5.93/5.8866/5.9294 (n=208)            -> 1.4%  = NOT
    WYHG 09:10:12  bucket 6.06/6.36/5.78/6.07  -> 50.0% = topping tail
                   leg    6.0762/6.12/6.0119/6.07 (n=717)            -> 40.5% = NOT
    VIOT 17:07:41  leg    1.46/1.49/1.43/1.4387 (n=720)               -> 50.0% = topping tail

(The OHLC above is the SHIPPED ``leg_print_candle`` run read-only against the live DB at each
decision instant -- publication-eligible prints only. The 5.9108 print the scout quoted as
WYHG's close was observed at 09:09:04.159 but only AVAILABLE at 09:09:04.945, after the
decision.) Two of the three live fires were a wick the position never saw. Across 35 TRAILING
legs in 14 days the bucket fired 17 times, 7 (41%) with the high before the entry fill.

The two fractions (0.50 / 1.0) are the candle's DEFINITION, not a tuned value; n >= 3 is
definitional (an upper wick needs a third print above both open and close). The window is the
leg, no clock, no N.

REVIEW FIXES (same PR, after #1385 landed on main):
  * ONE leg per tick -- the anchor is the G/D verdict's (`_exit_verdict_entry_at`). Since
    #1385 `entry_filled_at_utc` IS a recycle key and every adoption path pops it, so the
    first form's "later of the stamp and position.opened_at_utc" rested on a removed premise
    (and on an adoption path the position clock is the adoption instant, not a fill).
  * ONE as-of per tick -- the read takes `tick_as_of`, never a fresh clock later in the pass.
  * BOUNDED -- `bounded_fetchall` with the verdict's timeout (the event-tick spacing).
  * FRESH OR NOT JUDGED -- a close older than the shared 14.69 s print-age bound is
    `leg_candle_stale`; a 15-min-delayed feed is `no_publication_eligible_prints` first.
  * NAMED, NOT SILENT -- crypto / no anchor / delayed / stale / timed-out legs get ONE
    `live_topping_tail_unavailable` receipt per binding.
  * CORRECTED POPULATION -- with the shipped predicate + bound at every availability instant
    the leg candle fires 27 times (the scout's 28 read by observed_at only and counted a TPET
    fire the code cannot produce).
  * The no-`return` change is proven by a REAL `tick_live_session` pass on an EQUITY leg
    reading REAL prints: the chandelier, the OFI lock, the [58] reversal AND the stop-breach
    exit all run on the arming pass.

Runnable (the read tests shadow ``iqfeed_trade_ticks`` with a TEMP table on the test session;
the tick tests plant rows in the test DB's real table under unique symbols and delete them):
    TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/<db>_test \\
        pytest tests/test_topping_tail_leg_prints.py -v
"""
from __future__ import annotations

import ast
import inspect
import itertools
import json
import random
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.config import settings
from app.services.trading.momentum_neural import candles
from app.services.trading.momentum_neural import entry_gates as gates
from app.services.trading.momentum_neural import live_runner as lr
from tests.test_g4_grind_tick_wiring import (  # noqa: F401  (fixtures + the tick harness)
    _TT_ENTRY_S,
    _TT_LEG_PRINTS,
    _advancing_clock,
    _equity_symbol,
    _equity_tt_session,
    _events,
    _plant_leg,
    _run_tick,
    _seed,
    _tape as _grind_tape,
    planted_legs,
    tape_calls,
)


# ── a production-shaped tape on the test session ──────────────────────────────────

def _utc(v):
    if isinstance(v, datetime) and v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)
    return v


def _tape(db):
    """TEMP table that shadows ``iqfeed_trade_ticks`` for this transaction only."""
    db.execute(text(
        "CREATE TEMP TABLE iqfeed_trade_ticks (id bigserial PRIMARY KEY, symbol text, "
        "price double precision, observed_at timestamp, received_at timestamptz, "
        "available_at timestamptz) ON COMMIT DROP"
    ))

    def put(sym, price, at, *, received="same", available="same"):
        r = at if received == "same" else received
        a = r if available == "same" else available
        db.execute(
            text("INSERT INTO iqfeed_trade_ticks (symbol, price, observed_at, received_at, "
                 "available_at) VALUES (:s, :p, :e, :r, :a)"),
            {"s": sym, "p": price, "e": at, "r": _utc(r), "a": _utc(a)},
        )

    return put


VIOT_ENTRY = datetime(2026, 9, 10, 17, 4, 16, 359550)
VIOT_FIRE = datetime(2026, 9, 10, 17, 7, 41, 836170)


def _viot(put):
    put("VIOT", 1.60, VIOT_ENTRY - timedelta(minutes=3))          # before the fill: not the leg
    put("VIOT", 1.46, datetime(2026, 9, 10, 17, 4, 19, 339268))    # o
    put("VIOT", 1.47, datetime(2026, 9, 10, 17, 4, 50))
    put("VIOT", 1.49, datetime(2026, 9, 10, 17, 5, 13, 192091))    # h
    put("VIOT", 1.45, datetime(2026, 9, 10, 17, 6, 0))
    put("VIOT", 1.43, datetime(2026, 9, 10, 17, 7, 10))            # l
    put("VIOT", 1.4387, datetime(2026, 9, 10, 17, 7, 39, 950009))  # c
    put("VIOT", 1.55, VIOT_FIRE + timedelta(seconds=4))            # after the as-of


def _sess(symbol="VIOT", state="live_trailing", sid=21625):
    return SimpleNamespace(id=sid, symbol=symbol, state=state)


def _le(entry=VIOT_ENTRY, **extra):
    return {"entry_filled_at_utc": entry.replace(tzinfo=timezone.utc).isoformat(), **extra}


# ── leg_print_candle: the read ─────────────────────────────────────────────────────

def test_the_leg_candle_opens_at_the_fill_and_closes_at_the_as_of(db):
    put = _tape(db)
    _viot(put)
    leg = gates.leg_print_candle("viot", db=db, entry_at=VIOT_ENTRY, as_of=VIOT_FIRE)
    assert leg is not None
    assert (leg["o"], leg["h"], leg["l"], leg["c"], leg["n"]) == (1.46, 1.49, 1.43, 1.4387, 6)
    assert leg["high_at"] == "2026-09-10T17:05:13.192091"
    assert leg["first_at"] == "2026-09-10T17:04:19.339268"
    assert leg["last_at"] == "2026-09-10T17:07:39.950009"
    assert leg["entry_at"] == VIOT_ENTRY.isoformat()
    assert leg["as_of"] == VIOT_FIRE.isoformat()
    assert leg["window_kind"] == "leg_prints_since_entry_fill" == gates.LEG_PRINT_CANDLE_WINDOW_KIND
    assert leg["publication_basis"] == "conservative_received_and_available_as_of"
    # [29] freshness stamp: the close printed 1.886161 s before the decision
    assert leg["print_age_s"] == pytest.approx(1.886161, abs=1e-6)
    assert leg["print_age_bound_s"] == pytest.approx(
        float(settings.chili_momentum_g4_reentry_max_print_age_seconds))
    assert leg["print_stale"] is False
    assert leg["timeout_ms"] == 2000


def test_the_publication_predicate_is_the_sibling_one(db):
    """Same eligibility as ``high_print_in_window``: a print inside the window that was not
    RECEIVED, not AVAILABLE, available-before-received, on an infinite clock, or at a
    non-positive / non-finite price is not part of the candle."""
    put = _tape(db)
    t0 = datetime(2026, 9, 8, 9, 8, 37, 436055)
    as_of = t0 + timedelta(seconds=27)
    late = as_of + timedelta(seconds=1)
    put("WYHG", 5.89, t0 + timedelta(seconds=1))
    put("WYHG", 5.93, t0 + timedelta(seconds=5))
    put("WYHG", 5.8866, t0 + timedelta(seconds=9))
    put("WYHG", 5.9294, t0 + timedelta(seconds=26))
    # observed in the window, none of them eligible at the decision:
    put("WYHG", 9.01, t0 + timedelta(seconds=10), received=late, available=late)
    put("WYHG", 9.02, t0 + timedelta(seconds=10), available=late)
    put("WYHG", 9.03, t0 + timedelta(seconds=10), received=t0 + timedelta(seconds=12),
        available=t0 + timedelta(seconds=11))                      # available before received
    put("WYHG", 9.04, t0 + timedelta(seconds=10), available=None)
    put("WYHG", 9.05, t0 + timedelta(seconds=10), received=None)
    put("WYHG", 9.06, t0 + timedelta(seconds=10), received="-infinity", available="-infinity")
    put("WYHG", 9.07, t0 + timedelta(seconds=10), available="infinity")
    put("WYHG", float("nan"), t0 + timedelta(seconds=11))
    put("WYHG", float("inf"), t0 + timedelta(seconds=11))
    put("WYHG", 0.0, t0 + timedelta(seconds=11))
    put("WYHG", -1.0, t0 + timedelta(seconds=11))
    # the 5.9108 shape: observed before the as-of, available only after it
    put("WYHG", 5.9108, as_of - timedelta(milliseconds=190), available=late)
    leg = gates.leg_print_candle("WYHG", db=db, entry_at=t0, as_of=as_of)
    assert (leg["o"], leg["h"], leg["l"], leg["c"], leg["n"]) == (5.89, 5.93, 5.8866, 5.9294, 4)
    # the same prints, read once they HAVE arrived, close at 5.9108
    later = gates.leg_print_candle("WYHG", db=db, entry_at=t0, as_of=late + timedelta(seconds=1))
    assert later["c"] == 5.9108


def test_ties_on_observed_at_break_by_id_and_high_at_is_the_first_print_at_the_high(db):
    put = _tape(db)
    t0 = datetime(2026, 9, 10, 13, 0, 0)
    put("TIE", 2.00, t0 + timedelta(seconds=1))      # lower id at the first instant -> open
    put("TIE", 2.10, t0 + timedelta(seconds=1))
    put("TIE", 2.50, t0 + timedelta(seconds=3))      # the first print at the high
    put("TIE", 2.50, t0 + timedelta(seconds=4))
    put("TIE", 2.20, t0 + timedelta(seconds=9))
    put("TIE", 2.05, t0 + timedelta(seconds=9))      # higher id at the last instant -> close
    leg = gates.leg_print_candle("TIE", db=db, entry_at=t0, as_of=t0 + timedelta(seconds=10))
    assert (leg["o"], leg["h"], leg["l"], leg["c"], leg["n"]) == (2.00, 2.50, 2.00, 2.05, 6)
    assert leg["high_at"] == (t0 + timedelta(seconds=3)).isoformat()


def test_the_entry_anchor_is_one_instant_in_every_spelling(db):
    put = _tape(db)
    _viot(put)
    spellings = (
        VIOT_ENTRY,
        VIOT_ENTRY.replace(tzinfo=timezone.utc),
        VIOT_ENTRY.replace(tzinfo=timezone.utc).isoformat(),          # the le stamp's form
        "2026-09-10T13:04:16.359550-04:00",                           # the same instant, ET
        "2026-09-10T17:04:16.359550Z",
    )
    got = {
        json.dumps(gates.leg_print_candle("VIOT", db=db, entry_at=e, as_of=VIOT_FIRE),
                   sort_keys=True)
        for e in spellings
    }
    assert len(got) == 1


def test_the_as_of_defaults_through_the_replay_aware_clock(db, monkeypatch):
    """No as_of -> ``_tape_asof_default`` -> ``live_runner._utcnow`` (the sim clock in
    replay). A print after the sim instant is not in the candle. (The TRAILING block always
    threads its tick's one as-of; this default is for direct callers.)"""
    put = _tape(db)
    _viot(put)
    monkeypatch.setattr(lr, "_utcnow", lambda: VIOT_FIRE)
    leg = gates.leg_print_candle("VIOT", db=db, entry_at=VIOT_ENTRY)
    assert leg["c"] == 1.4387 and leg["h"] == 1.49 and leg["as_of"] == VIOT_FIRE.isoformat()


def test_an_empty_leg_is_no_candle_and_says_so(db):
    put = _tape(db)
    put("EMPTY", 3.00, VIOT_ENTRY - timedelta(seconds=30))   # only BEFORE the fill
    err: dict = {}
    assert gates.leg_print_candle("EMPTY", db=db, entry_at=VIOT_ENTRY, as_of=VIOT_FIRE,
                                  err=err) is None
    assert err["why"] == "no_publication_eligible_prints"
    assert gates.leg_print_candle("NOTAPE", db=db, entry_at=VIOT_ENTRY, as_of=VIOT_FIRE) is None


def test_every_unreadable_shape_is_no_candle_names_why_and_never_raises():
    db = object()   # never reached: each case returns before the query

    def why(symbol, *, db=db, entry_at=VIOT_ENTRY, as_of=VIOT_FIRE):
        err: dict = {}
        assert gates.leg_print_candle(symbol, db=db, entry_at=entry_at, as_of=as_of,
                                      err=err) is None
        return err.get("why")

    assert why("BTC-USD") == "no_equity_tape"
    assert why("VIOT", db=None) == "no_db"
    assert why(None) == "no_symbol"
    assert why("  ") == "no_symbol"
    assert why("VIOT", entry_at=None) == "entry_fill_anchor_missing"
    assert why("VIOT", entry_at="junk") == "entry_fill_anchor_missing"
    assert why("VIOT", entry_at=12345) == "entry_fill_anchor_missing"
    # inverted / empty bounds
    assert why("VIOT", entry_at=VIOT_FIRE, as_of=VIOT_ENTRY) == "as_of_not_after_entry"
    assert why("VIOT", entry_at=VIOT_FIRE, as_of=VIOT_FIRE) == "as_of_not_after_entry"

    class _Boom:
        def execute(self, *a, **kw):
            raise RuntimeError("table missing")

    assert why("VIOT", db=_Boom()) == "error"


# ── the read is BOUNDED (review fix: #1385's per-held-tick convention) ──────────────

class QueryCanceled(Exception):
    """Named like psycopg's statement-timeout error (the classifier reads the name)."""


class _TimedOutDB:
    """A session whose tape SELECT hits ``statement_timeout``: records every statement."""

    def __init__(self):
        self.statements: list[str] = []
        self.rolled_back = 0

    def begin_nested(self):
        outer = self

        class _SP:
            def rollback(self):
                outer.rolled_back += 1

        return _SP()

    def execute(self, stmt, params=None):
        s = str(stmt)
        self.statements.append(s)
        if "statement_timeout" in s:
            return None
        raise QueryCanceled("canceling statement due to statement timeout")


def test_the_leg_read_runs_under_a_statement_timeout_and_names_a_timeout():
    """FINDING (minor, x2): the leg is re-read in full on every TRAILING pass inside the
    row-locked tick. Measured read-only on the live DB with this SQL: 47,773 prints (BIAF
    09-09 12:00-13:00) 126-145 ms warm / 8,217 heap blocks; a 2 h / ~97k-print leg 14.7 s
    COLD. It now runs inside ``bounded_fetchall`` (SET LOCAL statement_timeout in a nested
    savepoint that is rolled back), and a timeout is a NAMED no-candle, not a stall."""
    db = _TimedOutDB()
    err: dict = {}
    assert gates.leg_print_candle("VIOT", db=db, entry_at=VIOT_ENTRY, as_of=VIOT_FIRE,
                                  err=err, timeout_ms=1234) is None
    assert err == {"why": "timeout", "error": "QueryCanceled"}
    assert db.statements[0] == "SET LOCAL statement_timeout = 1234"
    assert "FROM iqfeed_trade_ticks" in db.statements[1]
    assert db.rolled_back == 1            # the GUC never leaks into the tick's transaction


def test_the_trailing_read_uses_the_verdicts_own_timeout_and_names_leg_read_timeout():
    """The TRAILING read takes the G/D verdict's timeout (the loop's event-tick spacing), so
    the two per-tick tape reads share one bound; a timeout is `leg_read_timeout`."""
    cfg = lr._exit_verdict_settings()
    db = _TimedOutDB()
    out = lr._leg_topping_tail_read(db, _sess(), _le(), as_of=VIOT_FIRE)
    assert out["leg"] is None and out["shape"] is None
    u = out["unavailable"]
    assert u["binding"] == "leg_read_timeout"
    assert u["error"] == "QueryCanceled"
    assert u["timeout_ms"] == int(cfg["timeout_ms"]) == 2000
    assert db.statements[0] == f"SET LOCAL statement_timeout = {int(cfg['timeout_ms'])}"


def test_the_bounded_read_leaves_the_tick_transaction_usable_on_postgres(db):
    """On a real session the bounded read is a SAVEPOINT that is rolled back: the TEMP tape
    created before it survives, and the ``SET LOCAL`` does not leak into the transaction."""
    put = _tape(db)
    _viot(put)
    before = db.execute(text("SHOW statement_timeout")).scalar()
    leg = gates.leg_print_candle("VIOT", db=db, entry_at=VIOT_ENTRY, as_of=VIOT_FIRE,
                                 timeout_ms=1500)
    assert leg is not None and leg["timeout_ms"] == 1500
    assert db.execute(text("SHOW statement_timeout")).scalar() == before
    assert db.execute(text("SELECT count(*) FROM iqfeed_trade_ticks")).scalar() == 8


# ── freshness: a stale or delayed tape is NAMED, never read as "now" ─────────────────

def test_a_close_older_than_the_print_age_bound_is_stale_and_not_judged(db):
    """FINDING (minor): no print-age bound. A close 20 s old is over the shared 14.69 s
    bound (the p99 of 96,360 inter-print gaps -- the SAME bound as the G/D verdict's
    `stale`), so the candle is stamped stale and the TRAILING read does not judge it."""
    put = _tape(db)
    e = datetime(2026, 9, 10, 14, 0, 0)
    for s, px in ((1, 2.00), (5, 2.40), (9, 2.05), (10, 2.02)):
        put("STAL", px, e + timedelta(seconds=s))
    as_of = e + timedelta(seconds=30)                       # the close is 20 s old
    leg = gates.leg_print_candle("STAL", db=db, entry_at=e, as_of=as_of)
    assert leg["print_age_s"] == pytest.approx(20.0)
    assert leg["print_stale"] is True
    # it IS a topping-tail shape -- which is exactly why a stale one must not be judged
    assert candles.leg_topping_tail(leg)["is_topping_tail"] is True
    out = lr._leg_topping_tail_read(db, _sess("STAL"), _le(e), as_of=as_of)
    assert out["shape"] is None
    u = out["unavailable"]
    assert u["binding"] == "leg_candle_stale"
    assert u["print_age_s"] == pytest.approx(20.0)
    assert u["print_age_bound_s"] == pytest.approx(lr._exit_verdict_settings()["stale_bound_s"])


def test_a_fifteen_minute_delayed_feed_is_empty_then_stale(db):
    """TPET 09-10 (leg 13:22:35.69, 408 s): every print is published ~900 s after it was
    observed. At every instant of the leg NO print is publication-eligible -> named
    `no_publication_eligible_prints`; once they arrive the close is ~15 min old -> stale.
    The scout's population counted this leg as a fire (+$14.58) by reading observed_at only;
    the shipped read cannot produce it."""
    put = _tape(db)
    e = datetime(2026, 9, 10, 13, 22, 35, 690000)
    for s, px in ((2, 2.10), (40, 2.45), (100, 2.12), (380, 2.11)):
        at = e + timedelta(seconds=s)
        put("TPET", px, at, received=at + timedelta(seconds=900.5),
            available=at + timedelta(seconds=900.5))
    inside = e + timedelta(seconds=408)
    out = lr._leg_topping_tail_read(db, _sess("TPET"), _le(e), as_of=inside)
    assert out["unavailable"]["binding"] == "no_publication_eligible_prints"
    after = e + timedelta(seconds=380 + 900.5 + 1)
    out2 = lr._leg_topping_tail_read(db, _sess("TPET"), _le(e), as_of=after)
    assert out2["unavailable"]["binding"] == "leg_candle_stale"
    assert out2["unavailable"]["print_age_s"] > 900


# ── the anchor: the G/D verdict's leg, one definition per tick ─────────────────────

def test_the_leg_is_the_verdicts_leg_and_the_recycle_clears_the_stamp(db, monkeypatch):
    """FINDING (major, x2): the first form asserted `entry_filled_at_utc` was NOT a recycle
    key and anchored on the later of the stamp and `position.opened_at_utc`. #1385 made it a
    recycle key and pops it on every adoption path (`_clear_position_entry_anchor`), so the
    premise is gone; worse, on an adoption path `opened_at_utc` is the ADOPTION instant, so
    the topping tail and the G/D verdict judged two different legs on one tick. The leg is
    now `_exit_verdict_entry_at(le)` -- a position clock is never consulted."""
    assert "entry_filled_at_utc" in lr._RECYCLE_ENTRY_STATE_KEYS
    assert "position" in lr._RECYCLE_ENTRY_STATE_KEYS
    assert not hasattr(lr, "_leg_print_anchor"), "the later-of rule is gone"
    put = _tape(db)
    _viot(put)
    seen: list[dict] = []
    real = gates.leg_print_candle

    def _spy(symbol, **kw):
        seen.append(kw)
        return real(symbol, **kw)

    monkeypatch.setattr(gates, "leg_print_candle", _spy)
    # an adoption-shaped position whose clock is LATER than the fill: ignored
    le = _le(position={"opened_at_utc": (VIOT_ENTRY + timedelta(seconds=90)).isoformat()})
    out = lr._leg_topping_tail_read(db, _sess(), le, as_of=VIOT_FIRE)
    assert [k["entry_at"] for k in seen] == [lr._exit_verdict_entry_at(le)] == [VIOT_ENTRY]
    assert out["unavailable"] is None and out["shape"]["is_topping_tail"] is True
    # an adoption pops the anchor: the verdict's named fallback, and NO tape read
    lr._clear_position_entry_anchor(le)
    seen.clear()
    out = lr._leg_topping_tail_read(db, _sess(), le, as_of=VIOT_FIRE)
    assert not seen
    assert out["unavailable"]["binding"] == "entry_fill_anchor_missing"
    assert out["unavailable"]["binding"] == lr._exit_verdict_unsupported_binding(_sess(), le)


def test_crypto_is_the_verdicts_no_equity_tape_and_never_reads():
    out = lr._leg_topping_tail_read(object(), _sess("BTC-USD"), _le(), as_of=VIOT_FIRE)
    assert out["unavailable"]["binding"] == "no_equity_tape"
    assert out["leg"] is None and out["shape"] is None


def test_below_three_prints_is_named_not_judged(db):
    put = _tape(db)
    put("TWO", 1.00, VIOT_ENTRY + timedelta(seconds=1))
    put("TWO", 1.05, VIOT_ENTRY + timedelta(seconds=2))
    out = lr._leg_topping_tail_read(db, _sess("TWO"), _le(),
                                    as_of=VIOT_ENTRY + timedelta(seconds=3))
    assert out["unavailable"]["binding"] == "below_min_prints"
    assert out["unavailable"]["leg_n"] == 2


# ── the receipt: once per leg per binding ──────────────────────────────────────────

def test_an_unjudgeable_leg_is_said_once_per_binding_and_again_on_the_next_leg(monkeypatch):
    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(lr, "_emit", lambda db, sess, ev, payload: emitted.append((ev, dict(payload))))
    monkeypatch.setattr(lr, "_commit_le", lambda sess, le: None)
    le: dict = {}
    s = _sess("TPET")
    stale = {"binding": "leg_candle_stale", "entry_at": "2026-09-10T13:22:35.690000"}
    assert lr._leg_topping_tail_unavailable_once(None, s, le, stale, as_of=VIOT_FIRE) is True
    assert lr._leg_topping_tail_unavailable_once(None, s, le, stale, as_of=VIOT_FIRE) is False
    empty = {**stale, "binding": "no_publication_eligible_prints"}
    assert lr._leg_topping_tail_unavailable_once(None, s, le, empty, as_of=VIOT_FIRE) is True
    nxt = {**stale, "entry_at": "2026-09-10T13:40:00"}                 # a new leg
    assert lr._leg_topping_tail_unavailable_once(None, s, le, nxt, as_of=VIOT_FIRE) is True
    assert [p["binding"] for _, p in emitted] == [
        "leg_candle_stale", "no_publication_eligible_prints", "leg_candle_stale"]
    assert {ev for ev, _ in emitted} == {"live_topping_tail_unavailable"}
    for _, p in emitted:
        assert p["reported_once_per_leg_per_binding"] is True
        assert "no_arm" in p["fallback"]
        json.dumps(p, allow_nan=False)
    assert lr._TOPPING_TAIL_UNAVAILABLE_KEY in lr._RECYCLE_ENTRY_STATE_KEYS


# ── the measured live fires, as fixtures ──────────────────────────────────────────

def test_wyhg_0908_the_pre_entry_wick_does_not_arm(db):
    """The 15m bucket IS a topping tail and its high printed before the fill; the leg's own
    prints are not. Both 09-08 fires."""
    put = _tape(db)
    put("WYHG", 6.06, datetime(2026, 9, 8, 9, 0, 0, 39377))       # bucket open
    put("WYHG", 6.36, datetime(2026, 9, 8, 9, 3, 35, 381185))     # bucket high, pre-entry
    put("WYHG", 5.78, datetime(2026, 9, 8, 9, 7, 30))             # bucket low, pre-entry
    entry1 = datetime(2026, 9, 8, 9, 8, 37, 436055)
    fire1 = datetime(2026, 9, 8, 9, 9, 4, 350931)
    put("WYHG", 5.89, datetime(2026, 9, 8, 9, 8, 37, 456433))
    put("WYHG", 5.8866, datetime(2026, 9, 8, 9, 8, 45))
    put("WYHG", 5.93, datetime(2026, 9, 8, 9, 9, 1, 383087))
    put("WYHG", 5.9294, datetime(2026, 9, 8, 9, 9, 3, 776193))

    bucket = gates.leg_print_candle("WYHG", db=db, entry_at=datetime(2026, 9, 8, 9, 0), as_of=fire1)
    b = candles.topping_tail_shape(bucket["o"], bucket["h"], bucket["l"], bucket["c"])
    assert b["is_topping_tail"] is True and b["upper_wick_frac"] == pytest.approx(0.517241, abs=1e-6)
    assert bucket["high_at"] < entry1.isoformat()            # the wick is older than the position

    leg = gates.leg_print_candle("WYHG", db=db, entry_at=entry1, as_of=fire1)
    assert (leg["o"], leg["h"], leg["l"], leg["c"]) == (5.89, 5.93, 5.8866, 5.9294)
    shape = candles.leg_topping_tail(leg)
    assert shape["is_topping_tail"] is False
    assert shape["binding"] == "upper_wick_frac"
    assert shape["upper_wick_frac"] == pytest.approx(0.013825, abs=1e-6)
    out = lr._leg_topping_tail_read(db, _sess("WYHG"), _le(entry1), as_of=fire1)
    assert out["unavailable"] is None and out["shape"]["is_topping_tail"] is False

    # 09:10:12: the next leg, same pre-entry bucket high
    entry2 = datetime(2026, 9, 8, 9, 9, 42, 995629)
    fire2 = datetime(2026, 9, 8, 9, 10, 12, 792334)
    put("WYHG", 6.0762, datetime(2026, 9, 8, 9, 9, 43, 49452))
    put("WYHG", 6.12, datetime(2026, 9, 8, 9, 9, 55, 409850))
    put("WYHG", 6.0119, datetime(2026, 9, 8, 9, 10, 5))
    put("WYHG", 6.07, datetime(2026, 9, 8, 9, 10, 11, 733475))
    bucket2 = gates.leg_print_candle("WYHG", db=db, entry_at=datetime(2026, 9, 8, 9, 0), as_of=fire2)
    assert candles.is_topping_tail(bucket2["o"], bucket2["h"], bucket2["l"], bucket2["c"]) is True
    leg2 = gates.leg_print_candle("WYHG", db=db, entry_at=entry2, as_of=fire2)
    assert (leg2["o"], leg2["h"], leg2["l"], leg2["c"]) == (6.0762, 6.12, 6.0119, 6.07)
    shape2 = candles.leg_topping_tail(leg2)
    assert shape2["is_topping_tail"] is False
    assert shape2["upper_wick_frac"] == pytest.approx(0.40518, abs=1e-5)


def _fake_emit_env(monkeypatch, now):
    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(lr, "_emit", lambda db, sess, ev, payload: emitted.append((ev, dict(payload))))
    monkeypatch.setattr(lr, "_commit_le", lambda sess, le: None)
    monkeypatch.setattr(lr, "_utcnow", lambda: now)
    return emitted


def test_viot_0910_the_leg_candle_arms_and_the_receipt_names_the_binding(db, monkeypatch):
    put = _tape(db)
    _viot(put)
    le = _le()
    out = lr._leg_topping_tail_read(db, _sess(), le, as_of=VIOT_FIRE)
    assert out["unavailable"] is None
    leg, shape = out["leg"], out["shape"]
    assert shape["is_topping_tail"] is True
    # VIOT sits ON the definition: the upper wick is exactly half the range
    assert shape["upper_wick_frac"] == 0.5 and shape["wick_to_body"] == pytest.approx(1.408451)
    assert (shape["binding"], shape["binding_value"], shape["binding_definition"]) == (
        "upper_wick_frac", 0.5, 0.5)

    receipt = lr._leg_topping_tail_receipt(leg, shape)
    emitted = _fake_emit_env(monkeypatch, VIOT_FIRE)
    armed = lr._arm_opinion_exit(
        object(), _sess(), le, reason="topping_tail_runner_exit", prior_event="live_bailout",
        inputs={"bid": 1.43, "high_water_mark": 1.47, **receipt},
    )
    assert armed is True
    [(ev, payload)] = emitted
    assert ev == "live_opinion_exit_armed"
    assert payload["reason"] == "topping_tail_runner_exit"
    assert (payload["leg_o"], payload["leg_h"], payload["leg_l"], payload["leg_c"]) == (
        1.46, 1.49, 1.43, 1.4387)
    assert payload["leg_n"] == 6
    assert payload["leg_high_at"] == "2026-09-10T17:05:13.192091"
    assert payload["leg_entry_at"] == VIOT_ENTRY.isoformat()
    assert payload["leg_anchor_source"] == "entry_filled_at_utc"
    assert payload["leg_print_stale"] is False
    assert payload["leg_print_age_s"] == pytest.approx(1.886161, abs=1e-6)
    assert payload["leg_read_timeout_ms"] == 2000
    assert payload["window_kind"] == "leg_prints_since_entry_fill"
    assert payload["upper_wick_frac"] == 0.5
    assert payload["min_upper_wick_frac"] == 0.5 and payload["min_wick_to_body"] == 1.0
    assert payload["min_prints"] == 3
    assert payload["binding"] == "upper_wick_frac"
    assert payload["shape_definition"] == "candle_shape_definition_not_tuned"
    json.dumps(payload, allow_nan=False)          # JSONB-safe: no NaN / Infinity
    assert le["opinion_exit_armed"]["inputs"]["leg_c"] == 1.4387


# ── the shape and its definitional floor (pure) ───────────────────────────────────

def test_fewer_than_three_prints_can_never_be_a_topping_tail():
    """n >= 3 is DEFINITIONAL: with one or two prints the high is max(open, close), so the
    upper wick is 0. Exhaustive over a price grid, and the floor holds on a malformed leg."""
    grid = [1.0, 1.01, 1.5, 2.0, 5.93]
    for p in grid:
        assert candles.is_topping_tail(p, p, p, p) is False
    for p1, p2 in itertools.product(grid, repeat=2):
        assert candles.is_topping_tail(p1, max(p1, p2), min(p1, p2), p2) is False
    malformed = {"o": 10.0, "h": 11.0, "l": 9.95, "c": 10.1, "n": 2}   # a TT shape, n=2
    assert candles.is_topping_tail(10.0, 11.0, 9.95, 10.1) is True
    assert candles.leg_topping_tail(malformed) is None
    assert candles.leg_topping_tail({**malformed, "n": 3})["is_topping_tail"] is True
    assert candles.TOPPING_TAIL_MIN_PRINTS == 3


def test_no_leg_is_no_arm():
    assert candles.leg_topping_tail(None) is None
    assert candles.leg_topping_tail({}) is None
    assert candles.leg_topping_tail({"n": "x"}) is None
    assert candles.leg_topping_tail({"n": 5, "o": None, "h": 1, "l": 1, "c": 1}) is None


def test_the_shape_verdict_is_is_topping_tail_itself():
    rng = random.Random(20260911)
    for _ in range(2000):
        l = rng.uniform(1.0, 10.0)
        h = l + rng.choice([0.0, rng.uniform(0.0, 2.0)])
        o = rng.uniform(l, h) if h > l else l
        c = rng.uniform(l, h) if h > l else l
        shape = candles.topping_tail_shape(o, h, l, c)
        assert shape["is_topping_tail"] is candles.is_topping_tail(o, h, l, c)
        json.dumps(shape, allow_nan=False)


def test_at_the_definition_the_range_share_is_always_the_binding_condition():
    """With 0.50 / 1.0, ``upper >= range/2`` implies ``body <= range - upper <= upper``, so the
    wick-to-body condition can never be the one that refuses, and on a fire its slack
    (upper/body >= uwf/(1-uwf) >= 2*uwf) is never below the range share's. The receipt still
    reports both; ``binding`` is honestly ``upper_wick_frac`` at the definition."""
    rng = random.Random(5)
    for _ in range(2000):
        l = rng.uniform(1.0, 10.0)
        h = l + rng.uniform(0.001, 2.0)
        o, c = rng.uniform(l, h), rng.uniform(l, h)
        assert candles.topping_tail_shape(o, h, l, c)["binding"] == "upper_wick_frac"
    # a non-default definition can bind on the body (the general code path)
    viot = candles.topping_tail_shape(1.46, 1.49, 1.43, 1.4387, min_wick_to_body=3.0)
    assert viot["is_topping_tail"] is False and viot["binding"] == "wick_to_body"


def test_degenerate_candles_carry_a_json_safe_receipt():
    doji = candles.topping_tail_shape(10.0, 11.0, 9.9, 10.0)           # zero body
    assert doji["is_topping_tail"] is True and doji["wick_to_body"] is None
    assert doji["binding"] == "upper_wick_frac"
    flat = candles.topping_tail_shape(10.0, 10.0, 10.0, 10.0)
    assert flat["is_topping_tail"] is False and flat["binding"] == "zero_range"
    assert flat["upper_wick_frac"] is None
    for s in (doji, flat):
        json.dumps(s, allow_nan=False)
    assert candles.topping_tail_shape(None, 1, 1, 1) is None
    assert candles.topping_tail_shape("x", 1, 1, 1) is None


def test_the_candle_definition_is_unchanged():
    """The fractions are the definition, not a tuned value: the defaults of
    ``is_topping_tail`` ARE the named constants, and they did not move."""
    assert candles.TOPPING_TAIL_MIN_UPPER_WICK_FRAC == 0.50
    assert candles.TOPPING_TAIL_MIN_WICK_TO_BODY == 1.0
    sig = inspect.signature(candles.is_topping_tail)
    assert sig.parameters["min_upper_wick_frac"].default == 0.50
    assert sig.parameters["min_wick_to_body"].default == 1.0


def test_the_receipt_helper_never_raises():
    r = lr._leg_topping_tail_receipt(None, None)
    assert r["window_kind"] is None and r["binding"] is None
    json.dumps(r, allow_nan=False)


# ── the wiring in tick_live_session (structure) ───────────────────────────────────

def _topping_tail_if() -> ast.If:
    tick = ast.parse(inspect.getsource(lr.tick_live_session))
    hits = [
        n for n in ast.walk(tick)
        if isinstance(n, ast.If) and any(
            isinstance(c, ast.Constant) and c.value == "chili_momentum_exit_topping_tail_enabled"
            for c in ast.walk(n.test)
        )
    ]
    assert len(hits) == 1, len(hits)
    return hits[0]


def _names_called(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            out.add(f.id if isinstance(f, ast.Name) else getattr(f, "attr", ""))
    return out


def test_the_trailing_block_reads_the_leg_candle_not_a_clock_bucket():
    block = _topping_tail_if()
    called = _names_called(block)
    assert {"_leg_topping_tail_read", "_leg_topping_tail_unavailable_once",
            "_leg_topping_tail_receipt", "_arm_opinion_exit"} <= called
    assert not ({"_replay_aware_fetch_ohlcv_df", "fetch_ohlcv_df",
                 "topping_tail_from_df", "_leg_print_anchor"} & called), called
    names = {n.id for n in ast.walk(block) if isinstance(n, ast.Name)}
    assert "_entry_df" not in names
    consts = {n.value for n in ast.walk(block) if isinstance(n, ast.Constant)}
    assert "15m" not in consts


def test_the_read_is_handed_the_ticks_one_as_of():
    """FINDING (minor): the call passed no as_of, so `_tape_asof_default` took a FRESH
    `_utcnow()` ~12.7k lines after the quote -- after broker calls and verdict reads. Every
    read of the leg (the TRAILING block AND the OFI confirmer's fallback) is handed
    `tick_as_of`, the instant the bid and the G/D verdict were read at."""
    tick = ast.parse(inspect.getsource(lr.tick_live_session))
    calls = [n for n in ast.walk(tick) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "_leg_topping_tail_read"]
    assert len(calls) == 2, len(calls)
    for c in calls:
        kw = {k.arg: k.value for k in c.keywords}
        assert isinstance(kw.get("as_of"), ast.Name) and kw["as_of"].id == "tick_as_of"
    src = inspect.getsource(lr.tick_live_session)
    assert "topping_tail_from_df" not in src.replace(
        "Dati: `topping_tail_from_df` sa isang 1m bar", "")


def test_the_arming_pass_has_no_return_statement():
    """Structure pin (the behaviour is pinned by the real tick passes below)."""
    block = _topping_tail_if()
    assert not any(isinstance(n, ast.Return) for n in ast.walk(block))


def test_the_arm_carries_the_leg_receipt_on_both_branches():
    block = _topping_tail_if()
    src = ast.unparse(block)
    assert "**_tt_inputs" in src
    assert src.count("**_tt_inputs") == 2          # grind-hold receipt AND the arm inputs
    assert "'high_water_mark': _float_or_none(pos.get('high_water_mark'))" in src
    assert "g4_grind_hold_topping_tail" in src      # the G4 grind hold branch is kept


def test_the_flag_stays_live_and_on_and_no_knob_was_added():
    from app.config import Settings

    assert Settings.model_fields["chili_momentum_exit_topping_tail_enabled"].default is True
    assert {k for k in Settings.model_fields if "topping_tail" in k} == {
        "chili_momentum_exit_topping_tail_enabled",
    }
    assert not any("leg_print" in k or "leg_candle" in k for k in Settings.model_fields)


# ── the wiring in tick_live_session (BEHAVIOUR: real passes on an EQUITY leg) ────────

def _flags(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_exit_topping_tail_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_exit_tape_accel_reversal_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_exit_ofi_lock_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_exit_adaptive_equity_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_exit_candle_confirm_enabled", True)


def test_the_arming_pass_runs_the_trail_the_ofi_lock_and_the_reversal(
    db, monkeypatch, tape_calls, planted_legs
):
    """FINDING (minor): the no-`return` change was pinned by source text only. One REAL
    `tick_live_session` pass on an EQUITY TRAILING leg whose REAL leg candle (planted prints,
    real read) is a topping tail: the arm is written AND, on that same pass, the chandelier
    receipt path, the OFI lock and the [58] tape-accel reversal all run. On the old code the
    `return` ended the pass right after the arm."""
    _flags(monkeypatch)
    calls, box = tape_calls
    box["value"] = _grind_tape()
    sess, sym, t0 = _equity_tt_session(db, planted_legs, tag="A")
    rev: list[dict] = []
    ofi: list[dict] = []
    _run_tick(db, sess, symbol=sym, reversal_calls=rev, ofi_calls=ofi,
              clock=_advancing_clock(t0))

    armed = _events(db, sess, "live_opinion_exit_armed")
    assert armed and armed[-1]["reason"] == "topping_tail_runner_exit"
    assert (armed[-1]["leg_o"], armed[-1]["leg_h"], armed[-1]["leg_l"], armed[-1]["leg_c"]) == (
        10.50, 11.50, 10.40, 10.51)
    # ...and the SAME pass kept going below the arm:
    assert _events(db, sess, "live_exit_trail_authority"), "the trail section did not run"
    assert ofi, "the OFI exhaustion lock did not run on the arming pass"
    assert rev, "the [58] tape-accel reversal did not run on the arming pass"
    assert _events(db, sess, "live_tape_accel_reversal_exit")
    # the OFI confirmer's topping tail is the SAME leg candle (not a 1m wall-clock bucket;
    # the 1m frame is unreadable in this test, so the leg verdict alone decides)
    assert ofi[-1]["candle_exhaustion"] is True


def test_the_arming_pass_reaches_the_stop_breach_exit(
    db, monkeypatch, tape_calls, planted_legs
):
    """What the old `return` really skipped: EVERYTHING below the arm, including the
    stop-breach exit (`if bid <= stop_px:`). A leg whose bid is under its stop on the very
    pass the topping tail arms now starts the breach confirmation on that pass."""
    _flags(monkeypatch)
    calls, box = tape_calls
    box["value"] = _grind_tape()
    sess, sym, t0 = _equity_tt_session(db, planted_legs, tag="S", stop_price=10.60)
    _run_tick(db, sess, symbol=sym, bid=10.50, clock=_advancing_clock(t0))

    assert _events(db, sess, "live_opinion_exit_armed"), "the topping tail must arm"
    breach = _events(db, sess, "stop_breach_pending_confirm")
    assert breach, "the stop-breach exit must run on the arming pass"
    assert breach[-1]["bid"] == pytest.approx(10.50)


def test_the_candle_is_read_at_the_ticks_one_as_of(db, monkeypatch, tape_calls, planted_legs):
    """FINDING (minor): one as-of per tick. With a clock that moves on EVERY read, the leg
    read is handed exactly the as-of the G/D verdict was handed (`tick_as_of`) -- a fresh
    clock read later in the pass could never equal it."""
    _flags(monkeypatch)
    calls, box = tape_calls
    box["value"] = _grind_tape()
    sess, sym, t0 = _equity_tt_session(db, planted_legs, tag="O")
    verdicts: list[dict] = []
    legs: list[dict] = []
    _run_tick(db, sess, symbol=sym, verdict_calls=verdicts, leg_calls=legs,
              clock=_advancing_clock(t0))

    assert verdicts and legs
    assert len(legs) == 1, "one leg read per pass (the OFI confirmer reuses it)"
    assert legs[0]["as_of"] == verdicts[0]["as_of"]
    assert legs[0]["timeout_ms"] == lr._exit_verdict_settings()["timeout_ms"]
    armed = _events(db, sess, "live_opinion_exit_armed")[-1]
    assert armed["leg_as_of"] == verdicts[0]["as_of"].isoformat()


def test_a_delayed_feed_leg_is_named_and_never_arms_in_a_real_pass(
    db, monkeypatch, tape_calls, planted_legs
):
    """A 15-min-delayed name ([38]; TPET ~900 s) on a real pass: the same topping-tail
    prints, published 900.5 s late, are not eligible at the tick -> no arm, and ONE
    `live_topping_tail_unavailable` (`no_publication_eligible_prints`) says why."""
    _flags(monkeypatch)
    calls, box = tape_calls
    box["value"] = _grind_tape()
    sym = _equity_symbol("D")
    planted_legs.append(sym)
    t0 = datetime.utcnow().replace(microsecond=0)
    _plant_leg(db, symbol=sym, as_of=t0, publication_lag_s=900.5)
    sess = _seed(db, symbol=sym, entry_filled_at=t0 - timedelta(seconds=_TT_ENTRY_S))
    _run_tick(db, sess, symbol=sym, clock=_advancing_clock(t0))
    _run_tick(db, sess, symbol=sym, clock=_advancing_clock(t0 + timedelta(seconds=3)))

    assert not _events(db, sess, "live_opinion_exit_armed")
    rec = _events(db, sess, "live_topping_tail_unavailable")
    assert [r["binding"] for r in rec] == ["no_publication_eligible_prints"]
    assert rec[0]["entry_at"] == (t0 - timedelta(seconds=_TT_ENTRY_S)).isoformat()
