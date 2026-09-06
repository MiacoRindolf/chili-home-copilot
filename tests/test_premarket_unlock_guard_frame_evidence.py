"""07:00 ET SELLER-UNLOCK GUARD — evidence, not silence (2026-09-06, Ross Parity Bench).

MEASURED on the gate-15 baseline (@ 9383324b2): AEHL 2026-08-31 alpaca fired 41 consecutive
`abcd_break_tick_ok` candidates between 06:41 and 07:10 ET and every one was deferred at the
place path as ``premarket_seller_unlock_wait`` while the name printed 6.48-6.63 above a ~6.06
VWAP (Ross bought 6.54 at 06:56:54 and it printed 7.01 four minutes later). The guard requires
``le["entry_above_vwap"] is True`` — a stamp only the pullback / tape-hold / micro-pullback
trigger families write; every other family leaves it None, so the guard deferred on SILENCE.
356 such deferrals across 5 alpaca cases of the baseline.

The fix stamps the MEASURED side from the same canonical session frame the frontside sizing
tilt already reads on the pre-place tick (`_today_session_frame -> front_side_state`), only
when that frame has a VWAP. The guard's threshold is unchanged.

Runnable: pytest tests/test_premarket_unlock_guard_frame_evidence.py -v  (DB-free)
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta

import pandas as pd

from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural.entry_gates import _today_session_frame
from app.services.trading.momentum_neural.ross_momentum import front_side_state


def _frame(closes, *, vol=10_000):
    t0 = datetime(2026, 8, 31, 10, 30)  # 06:30 ET in UTC, one session
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
    # AEHL-shaped: a 5.90 -> 6.60 climb; the live mid sits above the cumulative VWAP
    st = front_side_state(_today_session_frame(_frame([5.90, 6.00, 6.10, 6.20, 6.30, 6.40, 6.50])), live_price=6.55)
    assert st.session_vwap is not None and st.session_vwap > 0
    assert st.above_vwap is True
    # and a fire BELOW the VWAP is measured as such (the guard then defers on evidence)
    st2 = front_side_state(_today_session_frame(_frame([6.60, 6.50, 6.40, 6.30, 6.20, 6.10, 6.00])), live_price=5.95)
    assert st2.session_vwap is not None and st2.above_vwap is False


def test_a_frame_without_volume_has_no_vwap_and_must_stamp_nothing():
    df = _frame([5.90, 6.00, 6.10, 6.20, 6.30, 6.40, 6.50], vol=0)
    st = front_side_state(_today_session_frame(df), live_price=6.55)
    assert st.session_vwap is None
    # front_side_state itself says True on an unknown VWAP ("don't penalize"); the stamp
    # therefore keys on session_vwap, not on above_vwap alone (source guard below).


def test_the_pre_place_stamp_sits_before_the_governed_place_and_keys_on_the_measured_vwap():
    src = inspect.getsource(lr)
    i = src.find('if le.get("entry_above_vwap") is None:')
    j = src.find("_pp_t_place = time.monotonic()", i)
    k = src.find("\n        res = _governed_place(", j)
    assert 0 < i < j < k, "the stamp must run on the pre-place tick, before the guard inside _governed_place"
    assert k - j < 800, "the stamp must sit immediately before the entry place call"
    block = src[i:j]
    assert "_su_today_frame(_su_frame), live_price=_float_or_none(mid)" in block
    assert '_su_vwap = _float_or_none(getattr(_su_state, "session_vwap", None))' in block
    assert "if _su_vwap is not None and _su_vwap > 0.0:" in block
    assert 'le["entry_above_vwap"] = bool(getattr(_su_state, "above_vwap"))' in block
    assert 'le["entry_above_vwap_source"] = "pre_place_session_frame"' in block
    assert '"live_entry_above_vwap_stamped_from_frame"' in block
    assert "_log.warning(" in block  # a failed read is visible, never a silent debug line


def test_the_guard_still_reads_the_stamp_and_its_threshold_is_unchanged():
    src = inspect.getsource(lr._strict_alpaca_rth_entry_window)
    assert '"chili_momentum_premarket_seller_unlock_guard_min"' in src
    assert 'if _su_live.get("entry_above_vwap") is not True:' in src
    assert '"reason": "premarket_seller_unlock_wait"' in src


def test_the_bench_receipt_carries_the_guard_evidence_and_the_stamp():
    import importlib.util
    import pathlib

    p = pathlib.Path(lr.__file__).resolve().parents[4] / "scripts" / "replay_v3_fsm_window.py"
    text = p.read_text(encoding="utf-8")
    i = text.find("_BENCH_PAYLOAD_KEYS = (")
    keys = text[i:text.find("\n)\n", i)]
    for k in ("above_vwap", "session_vwap", "entry_above_vwap", "et_minutes_from_0700", "guard_min"):
        assert f'"{k}"' in keys, k
