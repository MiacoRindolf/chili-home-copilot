"""07:00 ET SELLER-UNLOCK GUARD — evidence, not silence (2026-09-06, Ross Parity Bench; r2 after review).

MEASURED on the gate-15 baseline (@ 9383324b2): AEHL 2026-08-31 alpaca fired 41 consecutive
`abcd_break_tick_ok` candidates between 06:41 and 07:10 ET and every one was deferred at the
place path as ``premarket_seller_unlock_wait`` while the name printed 6.48-6.63 above a ~6.06
VWAP (Ross bought 6.54 at 06:56:54 and it printed 7.01 four minutes later). The guard requires
``le["entry_above_vwap"] is True`` — a stamp only the pullback / tape-hold / micro-pullback
trigger families write; every other family leaves it None, so the guard deferred on SILENCE.
356 such deferrals across 5 alpaca cases of the baseline.

r2 (review 2026-09-06): the frame evidence lives in its OWN key (``entry_above_vwap_frame``)
that only the 07:00 guard reads — ``entry_above_vwap`` is also the fail-closed input of the
no-halt vertical-chase budget (FIX-B) and must not be flipped by a place-time read; the block
runs for the Alpaca family only; a frame whose last bar is not TODAY (ET) stamps nothing.

Runnable: pytest tests/test_premarket_unlock_guard_frame_evidence.py -v  (DB-free)
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pandas as pd

from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural.entry_gates import _today_session_frame
from app.services.trading.momentum_neural.ross_momentum import front_side_state


def _frame(closes, *, vol=10_000, t0=datetime(2026, 8, 31, 10, 30, tzinfo=timezone.utc)):
    idx = pd.DatetimeIndex([t0 + timedelta(minutes=i) for i in range(len(closes))])
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [c * 1.01 for c in closes],
            "Low": [c * 0.99 for c in closes],
            "Close": closes,
            "Volume": [vol] * len(closes),
        },
        index=idx,
    )


def test_the_session_frame_measures_the_vwap_side_the_guard_needs():
    st = front_side_state(_today_session_frame(_frame([5.90, 6.00, 6.10, 6.20, 6.30, 6.40, 6.50])), live_price=6.55)
    assert st.session_vwap is not None and st.session_vwap > 0
    assert st.above_vwap is True
    st2 = front_side_state(_today_session_frame(_frame([6.60, 6.50, 6.40, 6.30, 6.20, 6.10, 6.00])), live_price=5.95)
    assert st2.session_vwap is not None and st2.above_vwap is False


def test_a_frame_without_volume_has_no_vwap_and_must_stamp_nothing():
    st = front_side_state(_today_session_frame(_frame([5.90, 6.00, 6.10, 6.20, 6.30, 6.40, 6.50], vol=0)), live_price=6.55)
    assert st.session_vwap is None


def test_r2_a_frame_whose_last_bar_is_not_today_et_is_refused():
    now = datetime(2026, 8, 31, 10, 55, tzinfo=timezone.utc)  # 06:55 ET on 08-31
    ok, dbg = lr.session_frame_is_today_et(_frame([5.9, 6.0, 6.1, 6.2, 6.3, 6.4, 6.5]), now)
    assert ok is True and dbg["frame_last_bar_date_et"] == "2026-08-31"
    yesterday = _frame([5.9, 6.0, 6.1, 6.2, 6.3, 6.4, 6.5], t0=datetime(2026, 8, 30, 19, 30, tzinfo=timezone.utc))
    ok, dbg = lr.session_frame_is_today_et(yesterday, now)
    assert ok is False and dbg["frame_last_bar_date_et"] == "2026-08-30" and dbg["today_et"] == "2026-08-31"
    # naive index is read as UTC; empty / non-datetime frames refuse
    naive = _frame([5.9, 6.0, 6.1, 6.2, 6.3, 6.4, 6.5], t0=datetime(2026, 8, 31, 10, 30))
    assert lr.session_frame_is_today_et(naive, now)[0] is True
    assert lr.session_frame_is_today_et(pd.DataFrame(), now)[0] is False
    assert lr.session_frame_is_today_et(None, now)[0] is False


def test_r2_the_stamp_is_alpaca_only_writes_its_own_key_and_keys_on_the_measured_vwap():
    src = inspect.getsource(lr)
    i = src.find('and le.get("entry_above_vwap_frame") is None')
    j = src.find("_pp_t_place = time.monotonic()", i)
    k = src.find("\n        res = _governed_place(", j)
    assert 0 < i < j < k, "the stamp must run on the pre-place tick, before the guard inside _governed_place"
    block = src[i - 400:j]
    assert "in ALPACA_EXECUTION_FAMILIES" in block  # alpaca only — the guard is not_applicable elsewhere
    assert "session_frame_is_today_et(" in block
    assert '_float_or_none(getattr(_su_state, "session_vwap", None))' in block
    assert "if _su_today_ok" in block  # no state, no VWAP, no stamp when the frame is not today
    assert "if _su_vwap is not None and _su_vwap > 0.0:" in block
    assert 'le["entry_above_vwap_frame"] = bool(getattr(_su_state, "above_vwap"))' in block
    assert 'le["entry_above_vwap"] =' not in block  # never the FIX-B input
    assert '"live_entry_above_vwap_stamped_from_frame"' in block
    assert '"live_entry_above_vwap_frame_not_today"' in block
    assert "_log.warning(" in block


def test_r2_the_guard_accepts_either_stamp_and_its_threshold_is_unchanged():
    src = inspect.getsource(lr._strict_alpaca_rth_entry_window)
    assert '"chili_momentum_premarket_seller_unlock_guard_min"' in src
    assert '_su_live.get("entry_above_vwap") is not True' in src
    assert '_su_live.get("entry_above_vwap_frame") is not True' in src
    assert '"reason": "premarket_seller_unlock_wait"' in src


def test_r2_the_frame_keys_are_cleared_with_the_trigger_stamp():
    src = inspect.getsource(lr)
    assert src.count('le.pop("entry_above_vwap", None)') == 3
    assert src.count('le.pop("entry_above_vwap_frame", None)') == 3
    assert src.count('le.pop("entry_above_vwap_source", None)') == 3
    for k in ("entry_above_vwap", "entry_above_vwap_frame", "entry_above_vwap_source", "entry_above_vwap_frame_stale_noted"):
        assert k in lr._RECYCLE_ENTRY_STATE_KEYS, k


def test_the_bench_receipt_carries_the_guard_evidence_and_the_stamp():
    """BEHAVIOUR, not source text ([E] merge, 2026-09-11).

    This used to text-search scripts/replay_v3_fsm_window.py for the ``_BENCH_PAYLOAD_KEYS = (``
    allow-list. That list was DELETED on 2026-09-07 (2fd0cd962 — the whole-payload contract in
    scripts/replay_bench_payload.py), so ``find`` returned -1, the "keys" slice was unrelated
    text, and the test failed on ``above_vwap`` while saying nothing about what the bench keeps.
    It now drives the driver's OWN receipt projection (``drv._bench_payload``) with the two
    payloads the lane writes for this guard and asserts the evidence comes back unchanged.
    """
    import pathlib
    import sys

    root = pathlib.Path(__file__).resolve().parents[1]
    for p in (str(root), str(root / "scripts")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import replay_v3_fsm_window as drv  # the script, same import as test_replay_v3_fsm_window_extensions

    # 1. the pre-place stamp event, built from the REAL frame read (AEHL shape: 06:55 ET 08-31)
    now = datetime(2026, 8, 31, 10, 55, tzinfo=timezone.utc)
    sess_df = _today_session_frame(_frame([5.90, 6.00, 6.10, 6.20, 6.30, 6.40, 6.50]))
    today_ok, frame_dbg = lr.session_frame_is_today_et(sess_df, now)
    st = front_side_state(sess_df, live_price=6.55)
    assert today_ok is True and st.session_vwap is not None and st.session_vwap > 0
    stamp = {
        "above_vwap": bool(st.above_vwap),
        "session_vwap": round(float(st.session_vwap), 6),
        "mid": 6.55,
        "source": "pre_place_session_frame",
        **frame_dbg,
    }
    assert "frame_last_bar_date_et" in stamp  # the frame debug really carries it
    kept_stamp = drv._bench_payload("live_entry_above_vwap_stamped_from_frame", stamp)

    # 2. the guard's own evidence on a deferral (``_strict_alpaca_rth_entry_window``'s return;
    #    the place path nests it as ``alpaca_entry_window``) — the trigger stamp SILENT (None),
    #    the frame stamp measured below VWAP. None must survive as None: "absent" is the finding.
    #    (event_type only feeds the load-bearing projection, which does not read it.)
    wait = {
        "reason": "premarket_seller_unlock_wait",
        "local_market_session": "premarket",
        "et_minutes_from_0700": -5.0,
        "guard_min": 15.0,
        "entry_above_vwap": None,
        "entry_above_vwap_frame": False,
    }
    kept_wait = drv._bench_payload("premarket_seller_unlock_wait", wait)
    kept_nested = drv._bench_payload("premarket_seller_unlock_wait", {
        "ok": False, "error": "premarket_seller_unlock_wait", "deferred": True,
        "pre_place_blocked": True, "alpaca_entry_window": wait,
    })

    for k in ("above_vwap", "session_vwap", "frame_last_bar_date_et"):
        assert k in kept_stamp and kept_stamp[k] == stamp[k], k
    for k in ("entry_above_vwap", "entry_above_vwap_frame"):
        assert k in kept_wait and kept_wait[k] == wait[k], k
    assert kept_wait["entry_above_vwap"] is None
    assert kept_nested["alpaca_entry_window"] == wait
    for kept in (kept_stamp, kept_wait, kept_nested):
        assert "_bench_trimmed" not in kept
