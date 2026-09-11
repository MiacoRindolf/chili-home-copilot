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

Runnable (the DB tests shadow ``iqfeed_trade_ticks`` with a TEMP table on the test session):
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

from app.services.trading.momentum_neural import candles
from app.services.trading.momentum_neural import entry_gates as gates
from app.services.trading.momentum_neural import live_runner as lr


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
    replay). A print after the sim instant is not in the candle."""
    put = _tape(db)
    _viot(put)
    monkeypatch.setattr(lr, "_utcnow", lambda: VIOT_FIRE)
    leg = gates.leg_print_candle("VIOT", db=db, entry_at=VIOT_ENTRY)
    assert leg["c"] == 1.4387 and leg["h"] == 1.49 and leg["as_of"] == VIOT_FIRE.isoformat()


def test_an_empty_leg_is_no_candle(db):
    put = _tape(db)
    put("EMPTY", 3.00, VIOT_ENTRY - timedelta(seconds=30))   # only BEFORE the fill
    assert gates.leg_print_candle("EMPTY", db=db, entry_at=VIOT_ENTRY, as_of=VIOT_FIRE) is None
    assert gates.leg_print_candle("NOTAPE", db=db, entry_at=VIOT_ENTRY, as_of=VIOT_FIRE) is None


def test_every_unreadable_shape_is_no_candle_and_never_raises():
    db = object()   # never reached: each case returns before the query
    assert gates.leg_print_candle("BTC-USD", db=db, entry_at=VIOT_ENTRY, as_of=VIOT_FIRE) is None
    assert gates.leg_print_candle("VIOT", db=None, entry_at=VIOT_ENTRY, as_of=VIOT_FIRE) is None
    assert gates.leg_print_candle(None, db=db, entry_at=VIOT_ENTRY, as_of=VIOT_FIRE) is None
    assert gates.leg_print_candle("  ", db=db, entry_at=VIOT_ENTRY, as_of=VIOT_FIRE) is None
    assert gates.leg_print_candle("VIOT", db=db, entry_at=None, as_of=VIOT_FIRE) is None
    assert gates.leg_print_candle("VIOT", db=db, entry_at="junk", as_of=VIOT_FIRE) is None
    assert gates.leg_print_candle("VIOT", db=db, entry_at=12345, as_of=VIOT_FIRE) is None
    # inverted / empty bounds
    assert gates.leg_print_candle("VIOT", db=db, entry_at=VIOT_FIRE, as_of=VIOT_ENTRY) is None
    assert gates.leg_print_candle("VIOT", db=db, entry_at=VIOT_FIRE, as_of=VIOT_FIRE) is None

    class _Boom:
        def execute(self, *a, **kw):
            raise RuntimeError("table missing")

    assert gates.leg_print_candle("VIOT", db=_Boom(), entry_at=VIOT_ENTRY, as_of=VIOT_FIRE) is None


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
    le = {"entry_filled_at_utc": VIOT_ENTRY.replace(tzinfo=timezone.utc).isoformat()}
    pos = {"opened_at_utc": (VIOT_ENTRY - timedelta(milliseconds=6)).isoformat(),
           "high_water_mark": 1.47}
    anchor, src = lr._leg_print_anchor(le, pos)
    assert (anchor, src) == (VIOT_ENTRY, "entry_filled_at_utc")
    leg = gates.leg_print_candle("VIOT", db=db, entry_at=anchor, as_of=VIOT_FIRE)
    shape = candles.leg_topping_tail(leg)
    assert shape["is_topping_tail"] is True
    # VIOT sits ON the definition: the upper wick is exactly half the range
    assert shape["upper_wick_frac"] == 0.5 and shape["wick_to_body"] == pytest.approx(1.408451)
    assert (shape["binding"], shape["binding_value"], shape["binding_definition"]) == (
        "upper_wick_frac", 0.5, 0.5)

    receipt = lr._leg_topping_tail_receipt(leg, shape, anchor_source=src)
    emitted = _fake_emit_env(monkeypatch, VIOT_FIRE)
    sess = SimpleNamespace(id=21625, state="live_trailing")
    armed = lr._arm_opinion_exit(
        object(), sess, le, reason="topping_tail_runner_exit", prior_event="live_bailout",
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


# ── the anchor: this leg, never the previous one ──────────────────────────────────

def test_the_normal_fill_path_anchors_on_the_fill_stamp():
    fill = datetime(2026, 9, 8, 9, 8, 37, 436055)
    le = {"entry_filled_at_utc": fill.replace(tzinfo=timezone.utc).isoformat()}
    pos = {"opened_at_utc": (fill - timedelta(milliseconds=5)).isoformat()}
    assert lr._leg_print_anchor(le, pos) == (fill, "entry_filled_at_utc")


def test_a_fill_stamp_older_than_the_position_is_not_the_anchor():
    """``entry_filled_at_utc`` survives a recycle and ``position`` does not; a stamp older
    than the position names the PREVIOUS leg -- the [5] defect again. The position's own
    clock wins."""
    assert "entry_filled_at_utc" not in lr._RECYCLE_ENTRY_STATE_KEYS
    assert "position" in lr._RECYCLE_ENTRY_STATE_KEYS
    prev_fill = datetime(2026, 9, 8, 9, 8, 37, 436055)
    opened = datetime(2026, 9, 8, 9, 9, 42, 990000)
    le = {"entry_filled_at_utc": prev_fill.replace(tzinfo=timezone.utc).isoformat()}
    assert lr._leg_print_anchor(le, {"opened_at_utc": opened.isoformat()}) == (
        opened, "position_opened_at_utc")


def test_no_stamp_and_no_opened_at_is_no_anchor_no_candle_no_arm():
    assert lr._leg_print_anchor({}, {}) == (None, None)
    assert lr._leg_print_anchor({}, None) == (None, None)
    assert lr._leg_print_anchor({"entry_filled_at_utc": "junk"}, {"opened_at_utc": "x"}) == (None, None)
    opened = datetime(2026, 9, 10, 17, 4, 16)
    assert lr._leg_print_anchor({"entry_filled_at_utc": "junk"},
                                {"opened_at_utc": opened.isoformat()}) == (
        opened, "position_opened_at_utc")
    assert gates.leg_print_candle("VIOT", db=object(), entry_at=None, as_of=VIOT_FIRE) is None
    assert candles.leg_topping_tail(None) is None


def test_the_receipt_helper_never_raises():
    r = lr._leg_topping_tail_receipt(None, None, anchor_source=None)
    assert r["window_kind"] is None and r["binding"] is None
    json.dumps(r, allow_nan=False)


# ── the wiring in tick_live_session ───────────────────────────────────────────────

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
    assert {"_leg_print_anchor", "leg_print_candle", "leg_topping_tail",
            "_leg_topping_tail_receipt", "_arm_opinion_exit"} <= called
    assert not ({"_replay_aware_fetch_ohlcv_df", "fetch_ohlcv_df",
                 "topping_tail_from_df"} & called), called
    names = {n.id for n in ast.walk(block) if isinstance(n, ast.Name)}
    assert "_entry_df" not in names
    consts = {n.value for n in ast.walk(block) if isinstance(n, ast.Constant)}
    assert "15m" not in consts


def test_the_arming_pass_does_not_return_before_the_chandelier():
    """Since #1377 the arm only writes a receipt; the old ``return`` on the arming pass only
    skipped the chandelier ratchet, the OFI lock and the [58] tape-accel reversal below."""
    block = _topping_tail_if()
    assert not any(isinstance(n, ast.Return) for n in ast.walk(block))
    src = inspect.getsource(lr.tick_live_session)
    i = src.find('"chili_momentum_exit_topping_tail_enabled"')
    j = src.find("_trailed = cushion_adaptive_trail_stop(", i)
    k = src.find("ofi_exhaustion_lock(", j)
    m = src.find("tape_accel_reversal_exit(", k)
    assert 0 < i < j < k < m
    # nothing between the arm and the chandelier ends the pass
    assert "return {" not in src[i:j]
    assert '"opinion_exit_armed": "topping_tail_runner_exit"' not in src


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
