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
    import pathlib

    p = pathlib.Path(lr.__file__).resolve().parents[4] / "scripts" / "replay_v3_fsm_window.py"
    text = p.read_text(encoding="utf-8")
    i = text.find("_BENCH_PAYLOAD_KEYS = (")
    keys = text[i:text.find("\n)\n", i)]
    for k in ("above_vwap", "session_vwap", "entry_above_vwap", "entry_above_vwap_frame", "frame_last_bar_date_et"):
        assert f'"{k}"' in keys, k
