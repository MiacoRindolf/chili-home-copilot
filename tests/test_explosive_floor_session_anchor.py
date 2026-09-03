"""THE E3 EXPLOSIVE-FLOOR CHANGE LEG WAS MEASURING A FIVE-DAY CHANGE.

THE DEFECT, traced line by line on origin/main 89cb0eb:

    live_runner.py:31539  _df_pb = fetch_ohlcv_df(sym, interval=_interval,
                                                  period="5d")
    live_runner.py:31596  _df_trig, _iv_trig = _df_pb, _interval
    live_runner.py:31661  momentum_pullback_trigger(_df_trig, ...)
    entry_gates.py:11925  -> pullback_break_confirmation(df, ...)
    entry_gates.py:9611   # "the 1m/5m day frames keep the check"
    entry_gates.py:9615   _sess_open = float(df["Open"].iloc[0])
    entry_gates.py:9617   _daily_change_pct = (close - _sess_open)/_sess_open*100
    entry_gates.py:9618   if _daily_change_pct < _change_floor:  # 10.0
                              return False, "below_explosive_floor_change", debug

``market_data._INTERVAL_MAX_PERIOD`` (:245-247) ALLOWS "5d" for 1m and 5m -- it
is not clamped -- and ``massive_client._period_to_dates`` maps "5d" to today
minus five CALENDAR days. ``pullback_break_confirmation`` never slices ``df`` to
the current session. The module's own ``_prior_day_close_from_frame`` docstring
(:2264-2267) states the premise outright: the frame "MAY dalang nakaraang araw
... katumbas ng period='5d' na hinihingi ng buhay na runner sa provider nito".

So ``df["Open"].iloc[0]`` is the open of a bar three to five sessions back, and
the quantity gated by ``chili_momentum_explosive_floor_change_pct`` is a FIVE-DAY
change wearing a day-change name -- while the SAME knob in
``ross_momentum.below_explosive_floor:931`` bounds the PREV-CLOSE-anchored day
change.

MEASURED (1,580 symbol-days, 11 sessions, 233,867 watch-set minutes; the 5-day
anchor reconstructed from the independent daily tape): the two anchors agree on
only 58.9% of minutes, and the median number of sessions inside the window is 4,
so the defect is live rather than degenerate. Both columns: the fix UNBLOCKS 256
symbol-days (23.3/session, 33.7% win, exp +0.04%) and BLOCKS 330 (30.0/session,
33.5% win, exp +0.02%) -- a wash on quality, -6.7 names/session on count.

VERIFIED against origin/main by swapping in the pristine ``entry_gates.py`` and
``config.py`` and re-running: 10 pass here, 8 FAIL there, and exactly 2 pass on
BOTH -- ``test_single_session_frame_is_unchanged_by_the_fix`` and
``test_micro_frame_still_skips_the_change_leg``, which are the invariants that
must not move in either direction.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.config import settings
from app.services.trading.momentum_neural import entry_gates as _eg

_ET = "America/New_York"


def _session_open_from_frame(df, cur=None):
    """Resolve the helper LAZILY so this file still COLLECTS on origin/main.

    A module-level import would turn every test here into one collection error,
    which proves only that a symbol is missing. Resolved lazily, the helper
    tests fail with a real message and -- more importantly -- the two
    gate-level column tests fail on their ASSERTIONS, which is the evidence
    that matters: they show the wrong anchor's decision, not an absent name.
    """
    fn = getattr(_eg, "_session_open_from_frame", None)
    if fn is None:
        pytest.fail(
            "entry_gates._session_open_from_frame is absent -- the E3 change leg "
            "is still anchored to df['Open'].iloc[0], the first bar of a "
            "period='5d' frame (live_runner.py:31539)."
        )
    return fn(df) if cur is None else fn(df, cur)


def _bars(day: str, opens, highs=None, lows=None, closes=None, start="09:30"):
    """One session of 1-minute bars indexed in ET."""
    n = len(opens)
    idx = pd.date_range(f"{day} {start}", periods=n, freq="1min", tz=_ET)
    o = list(opens)
    c = list(closes) if closes is not None else list(opens)
    h = list(highs) if highs is not None else [max(a, b) for a, b in zip(o, c)]
    lo = list(lows) if lows is not None else [min(a, b) for a, b in zip(o, c)]
    return pd.DataFrame(
        {"Open": o, "High": h, "Low": lo, "Close": c, "Volume": [1000] * n},
        index=idx,
    )


def _five_day_frame():
    """The shape the live runner actually hands the gate: FIVE sessions.

    Sessions 1-4 sell the name off from 10.00 down to 5.00; session 5 explodes
    from 5.00 to 9.00 (+80% TODAY). This is the RDAC/DFNS/MRNY/DAIC class the
    measurement names -- down all week, exploding today.
    """
    parts = [
        _bars("2026-08-25", [10.00, 9.50, 9.00]),
        _bars("2026-08-26", [8.80, 8.40, 8.00]),
        _bars("2026-08-27", [7.80, 7.20, 6.60]),
        _bars("2026-08-28", [6.40, 5.80, 5.20]),
        _bars("2026-08-31", [5.00, 6.50, 9.00]),
    ]
    return pd.concat(parts)


# ── the helper itself ────────────────────────────────────────────────────────

def test_session_open_is_todays_open_not_the_frames_first_bar():
    """The whole defect in one assertion: the frame's first bar is 10.00, five
    sessions before the trigger bar, while TODAY opened at 5.00."""
    df = _five_day_frame()
    assert float(df["Open"].iloc[0]) == pytest.approx(10.00)   # what main reads
    assert _session_open_from_frame(df) == pytest.approx(5.00)  # what today is


def test_session_open_follows_the_trigger_bar_not_the_end_of_the_frame():
    """AS-OF DISCIPLINE. ``cur`` is the trigger bar's integer position, so a
    replay evaluating an earlier bar must anchor to THAT bar's session, never to
    the last session present in the frame."""
    df = _five_day_frame()
    # position 7 sits in the 2026-08-27 session (3 bars/session: 0-2, 3-5, 6-8).
    assert _session_open_from_frame(df, 7) == pytest.approx(7.80)
    assert _session_open_from_frame(df, 0) == pytest.approx(10.00)
    assert _session_open_from_frame(df, len(df) - 1) == pytest.approx(5.00)


def test_session_open_handles_a_tz_naive_index_as_utc():
    """``_prior_day_close_from_frame`` treats a naive index as UTC; this must
    agree with it or the two helpers would disagree about which day it is."""
    df = _five_day_frame()
    naive = df.copy()
    naive.index = df.index.tz_convert("UTC").tz_localize(None)
    # 2026-08-31 09:30 ET == 13:30 UTC, same calendar day in ET after conversion.
    assert _session_open_from_frame(naive) == pytest.approx(5.00)


def test_session_open_fails_open_to_none_on_a_frame_it_cannot_read():
    """The helper never raises and never invents a number, so a caller that
    gets ``None`` falls back to the OLD path rather than to a new block. (This
    is a property of the new helper, so it fails on main for want of the
    symbol; the two tests that pass on BOTH are the single-session and
    micro-frame invariants below.)"""
    assert _session_open_from_frame(None) is None
    assert _session_open_from_frame(pd.DataFrame()) is None
    assert _session_open_from_frame(pd.DataFrame({"Close": [1.0]})) is None
    # A non-positive open is not an anchor.
    bad = _bars("2026-08-31", [0.0, 1.0, 2.0])
    assert _session_open_from_frame(bad) is None


# ── the gate ─────────────────────────────────────────────────────────────────

def _run_gate(df, *, flag: bool, entry_interval: str = "1m"):
    """Drive the E3 change leg through the confirmer.

    ⚠️ E3 IS KILL-SWITCHED OFF BY DEFAULT (``chili_momentum_explosive_floor_
    enabled``, config.py:5541-5545, ``default=False``) and it is OFF in the
    operator's own environment -- which is why nobody hit the wrong anchor: the
    gate does not run. The defect is LATENT, not live, and the tests must turn
    the gate on to exercise it at all. That is exactly the trap this change
    removes: switching E3 on today would silently apply a FIVE-DAY floor.
    """
    eg = _eg

    _old = {
        "chili_momentum_explosive_floor_enabled": getattr(
            settings, "chili_momentum_explosive_floor_enabled", False),
        "chili_momentum_explosive_floor_change_session_anchored": getattr(
            settings, "chili_momentum_explosive_floor_change_session_anchored", True),
    }
    object.__setattr__(settings, "chili_momentum_explosive_floor_enabled", True)
    object.__setattr__(
        settings, "chili_momentum_explosive_floor_change_session_anchored", flag
    )
    try:
        return eg.pullback_break_confirmation(
            df, entry_interval=entry_interval, symbol="TEST"
        )
    finally:
        for k, v in _old.items():
            object.__setattr__(settings, k, v)


def _today(scale: float = 1.0, day: str = "2026-08-31"):
    """One session that REACHES the E3 leg.

    A padded, wiggly base (so the ATR-scaled verticality cap is not degenerate
    and the pullback window cannot reach back into the PRIOR session), an
    impulse, a shallow pullback holding above EMA-9, and a break on a volume
    spike large enough to clear E3's own RVOL leg (floor 5.0).

    ``scale`` compresses the session around its 5.00 open: that is how the
    block-column case is built -- identical geometry, smaller move.

    The gate need not FIRE. E3 sits AHEAD of the verticality and later vetoes,
    so reaching it is enough to observe its verdict, and asserting on the
    reason string keeps the test from depending on gates it is not about.
    """
    wig = ([5.00, 5.06] * 10
           + [5.00, 5.12, 4.95, 5.18, 4.92, 5.20, 4.98, 5.22, 5.05, 5.25, 5.10, 5.30])
    o = wig[:-1] + [5.30, 5.55, 5.80, 5.72, 5.78]
    c = wig[1:] + [5.55, 5.80, 5.72, 5.78, 6.05]
    if scale != 1.0:
        o = [5.00 + (x - 5.00) * scale for x in o]
        c = [5.00 + (x - 5.00) * scale for x in c]
    h = [max(a, b) * 1.004 for a, b in zip(o, c)]
    lo = [min(a, b) * 0.996 for a, b in zip(o, c)]
    df = _bars(day, o, h, lo, c)
    df["Volume"] = [4000] * (len(o) - 5) + [16000, 18000, 7000, 8000, 60000]
    return df


def _prior_sessions(opens_by_day):
    """Flat prior sessions: open == close, so they contribute a price LEVEL and
    no structure. They exist only to move ``df["Open"].iloc[0]`` away from
    today's open, which is the whole defect."""
    out = []
    for d, o in opens_by_day:
        h = [x * 1.004 for x in o]
        lo = [x * 0.996 for x in o]
        df = _bars(d, o, h, lo, list(o))
        df["Volume"] = [3000] * len(o)
        out.append(df)
    return out


def test_gate_no_longer_refuses_a_name_exploding_today_after_a_week_of_selling():
    """THE UNBLOCK COLUMN, as a gate-level assertion.

    Sessions 1-4 walk the name down from 12.00 to 5.20; today it runs +21% off
    its own 5.00 open. Measured from the frame's FIRST bar -- 12.00, four
    sessions back -- the "day change" is deeply NEGATIVE, so origin/main returns
    ``below_explosive_floor_change``. Anchored to the session the name actually
    trades in, it clears the 10% floor.

    This is the RDAC/DFNS/MRNY/DAIC class: 256 symbol-days, 23.3 per session.
    """
    df = pd.concat(_prior_sessions([
        ("2026-08-25", [12.00, 11.40, 10.80]),
        ("2026-08-26", [10.40, 9.90, 9.40]),
        ("2026-08-27", [9.00, 8.40, 7.80]),
        ("2026-08-28", [7.20, 6.40, 5.60]),
    ]) + [_today(1.0)])

    _ok_old, reason_old, dbg_old = _run_gate(df, flag=False)
    assert reason_old == "below_explosive_floor_change"
    assert dbg_old["explosive_floor_change_anchor"] == "frame_first_bar"
    # The refused "day change" is measured against 12.00, four sessions back:
    # (6.05 - 12.00) / 12.00 = -49.58%, on a name that is +21% TODAY.
    assert dbg_old["explosive_floor_change_pct"] == pytest.approx(-49.58, abs=0.2)

    _ok_new, reason_new, dbg_new = _run_gate(df, flag=True)
    assert reason_new != "below_explosive_floor_change"
    assert dbg_new.get("explosive_floor_change_anchor") == "session_open"


def test_gate_now_refuses_a_name_that_ran_earlier_in_the_week_and_is_flat_today():
    """THE BLOCK COLUMN -- the reason this is not a one-directional loosening.

    The prior sessions climb 3.60 -> 4.90 and today adds only +7.4% off its own
    open. The five-day change clears 10% and the honest one does not, so the
    fix BLOCKS an entry origin/main admits. 330 symbol-days (30.0 per session)
    sit here, against 256 in the unblock column -- the change is net TIGHTENING
    by 6.7 names per session, not a widening.
    """
    df = pd.concat(_prior_sessions([
        ("2026-08-25", [3.60, 3.75, 3.90]),
        ("2026-08-26", [4.00, 4.15, 4.30]),
        ("2026-08-27", [4.40, 4.55, 4.70]),
        ("2026-08-28", [4.80, 4.85, 4.90]),
    ]) + [_today(0.35)])

    _ok_old, reason_old, _dbg_old = _run_gate(df, flag=False)
    assert reason_old != "below_explosive_floor_change"

    _ok_new, reason_new, dbg_new = _run_gate(df, flag=True)
    assert reason_new == "below_explosive_floor_change"
    assert dbg_new["explosive_floor_change_anchor"] == "session_open"
    # (5.3675 - 5.00) / 5.00 = +7.35%, under the 10.0 floor.
    assert dbg_new["explosive_floor_change_pct"] == pytest.approx(7.35, abs=0.2)


def test_flag_off_is_byte_identical_to_the_old_anchor():
    """THE KILL SWITCH. With the flag off the gate reads ``df["Open"].iloc[0]``
    exactly as before, and the payload names the anchor that decided. (Fails on
    main only because main emits no anchor key at all.)"""
    df = pd.concat(_prior_sessions([("2026-08-28", [7.20, 6.40, 5.60])])
                   + [_today(1.0)])
    _ok, _reason, dbg = _run_gate(df, flag=False)
    assert dbg["explosive_floor_change_anchor"] == "frame_first_bar"


def test_single_session_frame_is_unchanged_by_the_fix():
    """INVARIANT. When the frame holds ONE session the two anchors are the same
    number, so no decision may move. This is the guarantee that the change
    touches ONLY multi-session frames -- and the reason a replay fed a
    single-session frame sees byte-identical behaviour."""
    df = _today(1.0)
    old = _run_gate(df, flag=False)
    new = _run_gate(df, flag=True)
    assert old[0] == new[0]
    assert old[1] == new[1]
    assert old[2].get("explosive_floor_change_pct") == new[2].get(
        "explosive_floor_change_pct"
    )


def test_micro_frame_still_skips_the_change_leg():
    """INVARIANT (F1's own carve-out, preserved). A sub-minute frame spans a
    lookback window rather than a session, so the change leg is skipped
    entirely -- the fix must not resurrect it there."""
    df = pd.concat(_prior_sessions([("2026-08-28", [7.20, 6.40, 5.60])])
                   + [_today(1.0)])
    _ok, reason, dbg = _run_gate(df, flag=True, entry_interval="15s")
    assert reason != "below_explosive_floor_change"
    assert "explosive_floor_change_anchor" not in dbg


def test_e3_is_off_by_default_so_the_defect_is_latent_not_live():
    """The honest scope of this change, asserted rather than claimed.

    ``chili_momentum_explosive_floor_enabled`` is False by default
    (config.py:5542), so the wrong anchor is NOT costing entries today. What it
    is doing is making a kill-switched gate unsafe to switch on. This test
    pins that fact so the PR's claim and the code cannot drift apart.
    """
    from app.config import Settings

    assert Settings.model_fields["chili_momentum_explosive_floor_enabled"].default is False
    assert Settings.model_fields[
        "chili_momentum_explosive_floor_change_session_anchored"
    ].default is True
