"""Slow-recovery bar-count gate for the wick-reclaim entry (Ross HVM101 #008).

``wick_reclaim_confirmation`` re-enters the retrace into a big-upper-wick rejection candle
after a low-volume flush. Ross's HVM101 #008 rule on HOW MANY bars that recovery may take:

  * a wick rejection that RECOVERS within 1-3 bars is a valid reclaim;
  * the 4th bar counts ONLY when the tape is "really showing a lot of price action"
    (a high-rate-of-change, drying-up flush == ``flush_recedes`` True);
  * 5-6+ bars = a slow trickle = "usually more times than not invalid" = it CONFIRMS the
    rejection, NOT a reclaim.

The trigger already COMPUTED the rejection-bar offset (``cur - rej_idx``) and logged it as
``rejection_bar_offset`` but NEVER gated on it, so a slow 5-6-bar trickle wrongly fired.
The new gate (flag ``chili_momentum_wick_reclaim_slow_recovery_gate_enabled``, ONE base
``chili_momentum_wick_reclaim_max_recovery_bars`` = 4) REJECTS that invalid slow-recovery
case only. It is a pure QUALITY FILTER: it never loosens any existing wick-reclaim guard
(hot-tape / rejection / flush-dry / retrace all still run), and with the flag OFF the whole
gate is skipped -> byte-identical.

These are PURE-LOGIC tests on synthetic OHLCV frames. The indicator layer
(``compute_all_from_df`` -> atr / volume_ratio) is mocked so each leg (hot-tape, the
rejection scan window, the flush-dry-up proof) is controlled exactly; the gate's bar-count
logic runs for real. Adversarial matrix:

  * FAST  (rej offset 1-3)              -> FIRES   (gate is a no-op for fast recoveries)
  * SLOW  (rej offset 5-6)              -> REJECTED ("wick_reclaim_slow_recovery")
  * 4th bar + strong action (flush dry) -> FIRES   (the documented relaxation)
  * 4th bar + weak action (no dry-up)   -> REJECTED (relaxation withheld)
  * flag OFF                            -> the same FAST frame's behaviour is byte-identical
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest

from app.services.trading.momentum_neural import entry_gates
from app.services.trading.momentum_neural.entry_gates import wick_reclaim_confirmation

_GATES = "app.services.trading.momentum_neural.entry_gates"

# ── Geometry shared by every frame ─────────────────────────────────────────────────
# The rejection candle: a big UPPER wick (>= 0.5 of range) on an outsized range. Its high
# is the wick high (the reclaim target); its low seeds the flush low (the stop).
_REJ_HIGH = 11.00
_REJ_LOW = 10.00          # flush/wick low -> the structural stop
_REJ_OPEN = 10.10
_REJ_CLOSE = 10.20        # upper wick = 11.00 - 10.20 = 0.80 of a 1.00 range = 0.8 >= 0.5
# The reclaim bar (cur) re-enters the wick well past the 40% retrace: px ~10.70 ->
# retrace_frac = (10.70 - 10.00) / (11.00 - 10.00) = 0.70 >= 0.4.
_RECLAIM_CLOSE = 10.70

# atr / volume_ratio fixtures. ATR kept small so EVERY bar's range is "outsized" (range >=
# atr_abs); rvol[cur] high so the mandatory hot-tape gate passes on RVOL alone.
_ATR_ABS = 0.10
_HOT_RVOL = 5.0           # >= explosive rvol floor (3.0)


def _settings(*, gate_on: bool, vwap_K: int = 2, max_bars: int = 4) -> SimpleNamespace:
    """A minimal settings stand-in carrying exactly the knobs the trigger reads.

    ``vwap_K`` widens the rejection-scan look-back (rej_scan = K+1) so a rejection that sits
    5-6 bars behind ``cur`` is still DISCOVERED by the scanner -> the slow-recovery gate is
    what rejects it (not a too-narrow scan window quietly missing it)."""
    return SimpleNamespace(
        chili_momentum_wick_reclaim_entry_enabled=True,
        chili_momentum_wick_reclaim_min_wick_frac=0.5,
        chili_momentum_wick_reclaim_min_retrace_frac=0.4,
        chili_momentum_vwap_reclaim_min_below_bars=vwap_K,
        chili_momentum_explosive_atr_pct_floor=0.045,
        chili_momentum_explosive_rvol_floor=3.0,
        chili_momentum_wick_reclaim_slow_recovery_gate_enabled=gate_on,
        chili_momentum_wick_reclaim_max_recovery_bars=max_bars,
    )


def _frame(*, rej_offset: int, flush_dry: bool) -> tuple[pd.DataFrame, list, list]:
    """Build a wick-reclaim frame whose rejection bar sits ``rej_offset`` bars behind cur.

    Layout (n = rej_offset + leading_pad + 1):

        ... pad bars ... | REJECTION (big upper wick) | flush bars... | RECLAIM (cur) |

    The flush bars between the rejection and cur trade DOWN to the rejection low (so the
    flush low == the rejection low == the stop) and re-up only on the final reclaim bar.

    ``flush_dry`` controls the volume_ratio of the post-rejection flush bars:
      * True  -> flush bars carry LESS rel-vol than the rejection bar (a vacuum) -> the
                 ``flush_recedes`` strong-action proof holds;
      * False -> flush bars carry MORE rel-vol than the rejection bar -> the proof fails.
    Returns (df, atr_list, vr_list) so the caller can mock ``compute_all_from_df``.
    """
    lead = 8  # leading benign bars: n = lead + rej_offset + 1 stays >= 10 even at rej_offset=1
              # (the real frame length is set on line ~99; the len(df)<10 guard fires first otherwise)
    n = lead + 1 + rej_offset + 1  # pad + rejection + (offset-1 flush) + reclaim? see below
    # Index math: cur = n-1; rejection at idx (cur - rej_offset).
    n = lead + rej_offset + 1
    cur = n - 1
    rej_idx = cur - rej_offset

    highs, lows, opens, closes, vols = [], [], [], [], []
    atr_list, vr_list = [], []
    rej_vr = 4.0           # rejection-bar rel-vol baseline
    flush_vr = 2.0 if flush_dry else 6.0  # below/above the rejection baseline

    for i in range(n):
        atr_list.append(_ATR_ABS)
        if i < rej_idx:
            # benign lead-in bars (small range, not a big-wick rejection)
            opens.append(10.00); closes.append(10.05)
            highs.append(10.08); lows.append(9.98)
            vols.append(1.0); vr_list.append(1.0)
        elif i == rej_idx:
            opens.append(_REJ_OPEN); closes.append(_REJ_CLOSE)
            highs.append(_REJ_HIGH); lows.append(_REJ_LOW)
            vols.append(1.0); vr_list.append(rej_vr)
        elif i < cur:
            # FLUSH bars: trade down to the rejection low (the vacuum), modest range.
            opens.append(10.30); closes.append(10.10)
            highs.append(10.35); lows.append(_REJ_LOW)
            vols.append(1.0); vr_list.append(flush_vr)
        else:
            # RECLAIM bar (cur): re-enters the wick. rvol high -> hot-tape passes.
            opens.append(10.30); closes.append(_RECLAIM_CLOSE)
            highs.append(10.75); lows.append(10.25)
            vols.append(1.0); vr_list.append(_HOT_RVOL)

    df = pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": vols}
    )
    return df, atr_list, vr_list


def _run(df, atr_list, vr_list, settings_obj):
    """Invoke the trigger with compute_all_from_df + settings mocked."""
    with patch.object(entry_gates, "settings", settings_obj), patch.object(
        entry_gates,
        "compute_all_from_df",
        return_value={"atr": atr_list, "volume_ratio": vr_list},
    ):
        return wick_reclaim_confirmation(df, entry_interval="1m", live_price=_RECLAIM_CLOSE)


# ── Sanity: the baseline FAST frame fires with the gate OFF (control) ────────────────
def test_fast_recovery_fires_gate_off():
    df, atr_list, vr_list = _frame(rej_offset=2, flush_dry=True)
    ok, reason, dbg = _run(df, atr_list, vr_list, _settings(gate_on=False))
    assert ok is True, (reason, dbg)
    assert reason == "wick_reclaim"


# ── FAST (1-3 bars): the gate is a no-op -> still fires ─────────────────────────────
@pytest.mark.parametrize("offset", [1, 2, 3])
def test_fast_recovery_fires_with_gate_on(offset):
    df, atr_list, vr_list = _frame(rej_offset=offset, flush_dry=True)
    ok, reason, dbg = _run(df, atr_list, vr_list, _settings(gate_on=True))
    assert ok is True, (offset, reason, dbg)
    assert reason == "wick_reclaim"


# ── SLOW (5-6 bars): rejected as a slow trickle ────────────────────────────────────
@pytest.mark.parametrize("offset", [5, 6])
def test_slow_recovery_rejected(offset):
    # widen the scan look-back so the rejection 5-6 bars back is still DISCOVERED, proving
    # the slow-recovery GATE (not a narrow scan) is what rejects it.
    df, atr_list, vr_list = _frame(rej_offset=offset, flush_dry=True)
    ok, reason, dbg = _run(
        df, atr_list, vr_list, _settings(gate_on=True, vwap_K=offset + 1)
    )
    assert ok is False, (offset, reason, dbg)
    assert reason == "wick_reclaim_slow_recovery", dbg
    assert dbg.get("rejection_bar_offset") == offset
    assert dbg.get("max_recovery_bars") == 4


# ── 4th bar WITH strong action (flush dries up) -> the documented relaxation FIRES ──
def test_fourth_bar_with_strong_action_fires():
    df, atr_list, vr_list = _frame(rej_offset=4, flush_dry=True)
    ok, reason, dbg = _run(
        df, atr_list, vr_list, _settings(gate_on=True, vwap_K=5)
    )
    assert ok is True, (reason, dbg)
    assert reason == "wick_reclaim"


# ── 4th bar WITHOUT strong action (flush does not dry up) -> relaxation withheld ────
def test_fourth_bar_without_strong_action_rejected():
    df, atr_list, vr_list = _frame(rej_offset=4, flush_dry=False)
    ok, reason, dbg = _run(
        df, atr_list, vr_list, _settings(gate_on=True, vwap_K=5)
    )
    assert ok is False, (reason, dbg)
    # the slow-recovery gate fires BEFORE the existing flush-dry guard, so the 4th-bar
    # weak-action case is attributed to the bar-count gate (the relaxation was withheld).
    assert reason == "wick_reclaim_slow_recovery", dbg
    assert dbg.get("strong_action") is False
    assert dbg.get("rejection_bar_offset") == 4


# ── Flag OFF == byte-identical: the slow frame that the gate WOULD reject is unchanged
def test_flag_off_byte_identical_on_slow_frame():
    # a 5-bar slow recovery: gate ON rejects it; gate OFF must behave EXACTLY as the
    # pre-gate trigger did (the gate code is skipped entirely).
    df, atr_list, vr_list = _frame(rej_offset=5, flush_dry=True)

    ok_off, reason_off, dbg_off = _run(
        df, atr_list, vr_list, _settings(gate_on=False, vwap_K=6)
    )
    ok_on, reason_on, dbg_on = _run(
        df, atr_list, vr_list, _settings(gate_on=True, vwap_K=6)
    )

    # gate ON rejects the slow trickle...
    assert ok_on is False and reason_on == "wick_reclaim_slow_recovery"
    # ...gate OFF NEVER returns the slow-recovery reason (the gate is fully skipped). The
    # pre-gate trigger fires on this otherwise-valid frame.
    assert reason_off != "wick_reclaim_slow_recovery"
    assert ok_off is True and reason_off == "wick_reclaim"
    # and OFF never stamps the gate-only debug keys.
    assert "max_recovery_bars" not in dbg_off
    assert "strong_action" not in dbg_off
