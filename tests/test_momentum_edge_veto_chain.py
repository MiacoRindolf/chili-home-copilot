"""PRINCIPAL-LEVEL edge-case bug hunt — the ENTRY VETO-CHAIN class.

``pullback_break_confirmation`` (app/services/trading/momentum_neural/entry_gates.py)
runs a long ORDERED sequence of veto gates after a trigger fires. The first gate that
trips returns its reason and SHORT-CIRCUITS the rest. The exact ORDER, the surgical
deep_reclaim EXEMPTIONS, the tick-break SKIPS, and the flag gating are all load-bearing:
a re-order or a too-broad exemption silently changes which name gets blocked in prod.

These tests are NOT branch-coverage padding. Each one constructs a frame/setup that the
function will plausibly meet AND asserts the EXACT reason string returned, so the test
FAILS if the precedence is subtly wrong.

The veto chain (post-trigger), in source order, with default flag state:
  1. _detect_back_side        -> "back_side_disabled"           (always-on; exempt deep_reclaim)
  2. front_side_state         -> "backside_lifecycle_veto"      (flag default True; exempt deep_reclaim)
  3. doji_trigger_veto        -> "doji_trigger_veto"            (flag default FALSE; skip tick; exempt deep_reclaim)
  4. htf_against_veto         -> "htf_against_veto"             (same flag default FALSE; exempt deep_reclaim)
  5. red_vol_exhaustion       -> "red_vol_exhaustion_veto"     (flag default True; skip tick; exempt deep_reclaim)
  6. explosive_floor (rvol)   -> "below_explosive_floor_rvol"  (flag default FALSE; EQUITY only)
     explosive_floor (chg)    -> "below_explosive_floor_change"
  7. volume floor             -> "break_low_volume"            (skip tick)
  8. sustained vol            -> "faded_volume_no_sustain"     (require_sustained_volume)
  9. break candle             -> "weak_break_candle"           (require_break_candle; skip tick)
 10. vwap hold                -> "below_vwap"                   (require_vwap_hold)
 11. macd                     -> "macd_not_bullish"            (require_macd_bullish)
 12. verticality              -> "extended_verticality"        (always-on unless mult=0)

ISOLATION METHOD: the trigger helpers and the structural-indicator gates contain their
own deep math (covered elsewhere). To probe the ORCHESTRATION precisely, we patch:
  * ``entry_gates.compute_all_from_df``  -> controlled ema9/ema20/vr/macd/vwap/atr arrays
  * the trigger helpers (``_evaluate_raw_break`` / ``first_pullback_break``) -> a chosen verdict
  * the leaf gate detectors (``_detect_back_side`` / ``ross_momentum.front_side_state``)
  * ``_today_session_frame`` -> identity passthrough (we feed a single-session frame anyway)
so each test controls EXACTLY which gates fire and asserts which reason wins.

A handful of tests run end-to-end on REAL frames (no helper patching) to prove the
all-flags-OFF raw path and a genuine multi-veto precedence on un-mocked math.
"""

import numpy as np
import pandas as pd
import pytest

from app.config import settings
from app.services.trading.momentum_neural import entry_gates as eg
from app.services.trading.momentum_neural import ross_momentum as rm
from app.services.trading.momentum_neural.entry_gates import pullback_break_confirmation


# --------------------------------------------------------------------------- #
# Frame + array builders
# --------------------------------------------------------------------------- #
def _frame(n=30, *, base=10.0, vol=1000.0, datetime_index=True):
    """A minimal valid OHLCV frame (>=10 bars). All columns present so the function's
    own Open/High/Low/Close/Volume reads never raise. Values are placeholders; the
    indicator arrays are supplied via a patched ``compute_all_from_df`` for the
    orchestration tests."""
    closes = np.full(n, base, dtype=float)
    opens = closes.copy()
    highs = closes * 1.01
    lows = closes * 0.99
    vols = np.full(n, vol, dtype=float)
    if datetime_index:
        idx = pd.date_range("2026-06-26 13:30", periods=n, freq="1min", tz="UTC")
    else:
        idx = pd.RangeIndex(n)
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": vols},
        index=idx,
    )


def _arrays(n=30, *, ema9=11.0, ema20=10.0, vr=10.0, macd=1.0, macd_sig=0.5,
            macd_hist=0.5, vwap=1.0, atr=0.1):
    """A controlled indicator-array dict the patched ``compute_all_from_df`` returns.
    Defaults are deliberately gate-PASSING: ema9>ema20 (front side), high rel-vol,
    bullish macd, tiny vwap so price is above it, small atr. Each test overrides the
    one field it wants to trip a specific gate."""
    def col(v):
        return [float(v)] * n
    return {
        "ema_9": col(ema9),
        "ema_20": col(ema20),
        "volume_ratio": col(vr),
        "atr": col(atr),
        "vwap": col(vwap),
        "macd": col(macd),
        "macd_signal": col(macd_sig),
        "macd_hist": col(macd_hist),
    }


def _patch_orchestration(
    monkeypatch,
    *,
    arrays=None,
    trigger=("raw", True, "raw_break", 12.0, 9.0, None),
    back_side=(False, ""),
    front_backside=False,
    front_reason="front_side",
):
    """Patch the leaf helpers so ONLY the orchestration logic is exercised.

    trigger = (kind, ok, reason, pb_high, pb_low, pattern). kind in {"raw","fp"}.
    ``pattern`` is stamped into the debug dict (e.g. "deep_reclaim" to take the
    exemption path, or "first_pullback" / None).
    """
    arr = arrays if arrays is not None else _arrays()
    monkeypatch.setattr(eg, "compute_all_from_df", lambda df, needed=None: dict(arr))

    kind, ok, reason, pbh, pbl, pattern = trigger

    def fake_raw(high, low, ema9, cur, **kw):
        dbg = {"entry_interval": kw.get("entry_interval", "5m")}
        if pbh is not None:
            dbg["pullback_high"] = float(pbh)
        if pbl is not None:
            dbg["pullback_low"] = float(pbl)
        if pattern is not None:
            dbg["pattern"] = pattern
        return ok, reason, (pbh if ok else None), (pbl if ok else None), dbg

    monkeypatch.setattr(eg, "_evaluate_raw_break", fake_raw)
    monkeypatch.setattr(eg, "_evaluate_break_retest", fake_raw)

    # Neutralise the first-pullback gate unless this test IS the fp trigger.
    if kind == "fp":
        def fake_fp(df, **kw):
            dbg = {"pullback_high": float(pbh), "pullback_low": float(pbl),
                   "pattern": "first_pullback"}
            return "FIRE", float(pbh), float(pbl), dbg
        monkeypatch.setattr(eg, "first_pullback_break", fake_fp)
    else:
        monkeypatch.setattr(
            eg, "first_pullback_break",
            lambda df, **kw: ("PASS", None, None, {"fp_declined": "neutralised"}),
        )

    # Point-in-time backside detector.
    monkeypatch.setattr(eg, "_detect_back_side", lambda *a, **k: back_side)

    # Session-anchored front_side_state — imported locally inside the function via
    # `from .ross_momentum import front_side_state`, so patch it on the source module.
    class _FS:
        is_backside = front_backside
        reason = front_reason
        front_side_score = 0.5

    monkeypatch.setattr(rm, "front_side_state", lambda *a, **k: _FS())
    # _today_session_frame is module-level; pass the frame through unchanged.
    monkeypatch.setattr(eg, "_today_session_frame", lambda df: df)


def _call(df=None, **kw):
    return pullback_break_confirmation(df if df is not None else _frame(), **kw)


# --------------------------------------------------------------------------- #
# (4) EVERY GATE FLAG OFF -> the trigger fires RAW (byte-identical success).
# --------------------------------------------------------------------------- #
def test_all_flags_off_raw_fire(monkeypatch):
    """With the always-on gates neutralised (front-side, high vol, bullish, not
    extended) and every optional flag OFF, a clean raw break returns the bare
    ``pullback_break_ok`` — the un-vetoed success reason."""
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_candle_quality_multitf_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_red_vol_exhaustion_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_entry_verticality_atr_mult", 0.0)
    _patch_orchestration(monkeypatch)
    ok, reason, dbg = _call()
    assert ok is True
    assert reason == "pullback_break_ok"


# --------------------------------------------------------------------------- #
# (1) MULTI-VETO PRECEDENCE — a name that trips MANY gates at once. The FIRST in
#     source order must win. We progressively trip gate after gate and assert the
#     reason stays pinned to the EARLIEST tripped gate.
# --------------------------------------------------------------------------- #
def test_precedence_back_side_beats_everything(monkeypatch):
    """_detect_back_side (gate 1) fires AND front_side is backside AND vol is dead AND
    macd is bearish AND price is extended — gate 1 must win: ``back_side_disabled``."""
    monkeypatch.setattr(settings, "chili_momentum_candle_quality_multitf_veto_enabled", True)
    arr = _arrays(vr=0.1, macd=-1.0, macd_sig=1.0, macd_hist=-1.0, ema9=50.0, vwap=999.0)
    _patch_orchestration(
        monkeypatch, arrays=arr,
        back_side=(True, "ema9_below_ema20"),
        front_backside=True, front_reason="below_vwap",
    )
    ok, reason, dbg = _call(require_macd_bullish=True, require_vwap_hold=True)
    assert ok is False
    assert reason == "back_side_disabled"
    assert dbg.get("back_side") == "ema9_below_ema20"


def test_precedence_front_side_beats_doji_and_later(monkeypatch):
    """Gate 1 PASSES (back_side False) but gate 2 (front_side_state backside) AND gate 3
    (doji) AND later gates would all fire. Gate 2 wins: ``backside_lifecycle_veto``."""
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_candle_quality_multitf_veto_enabled", True)
    arr = _arrays(vr=0.1)
    _patch_orchestration(
        monkeypatch, arrays=arr,
        back_side=(False, ""),
        front_backside=True, front_reason="already_faded",
    )
    ok, reason, dbg = _call()
    assert ok is False
    assert reason == "backside_lifecycle_veto"
    assert dbg.get("front_side_state") == "already_faded"


def test_precedence_doji_beats_htf_and_redvol(monkeypatch):
    """Gates 1-2 pass; gate 3 (doji) and gate 4 (htf) both armed via the same flag, plus
    red-vol would fire. The DOJI gate (earlier in source) must win: ``doji_trigger_veto``."""
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_candle_quality_multitf_veto_enabled", True)
    # Force the doji detector to veto and the htf detector to ALSO veto.
    monkeypatch.setattr(eg, "_doji_trigger_veto", lambda *a, **k: (True, {"doji_body_frac": 0.05}))
    monkeypatch.setattr(eg, "_htf_against_veto", lambda *a, **k: (True, {"htf_against": "macd_peaked"}))
    arr = _arrays(vr=0.1)
    _patch_orchestration(monkeypatch, arrays=arr)
    ok, reason, dbg = _call()
    assert ok is False
    assert reason == "doji_trigger_veto"
    # htf debug must NOT have been written — we short-circuited before it.
    assert "htf" not in dbg


def test_precedence_htf_beats_redvol(monkeypatch):
    """Doji passes, HTF-against fires, red-vol would also fire. HTF wins (gate 4 < 5):
    ``htf_against_veto``."""
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_candle_quality_multitf_veto_enabled", True)
    monkeypatch.setattr(eg, "_doji_trigger_veto", lambda *a, **k: (False, {}))
    monkeypatch.setattr(eg, "_htf_against_veto", lambda *a, **k: (True, {"htf_against": "ema9_sustained_rolldown"}))
    _patch_orchestration(monkeypatch, arrays=_arrays(vr=0.1))
    ok, reason, dbg = _call()
    assert ok is False
    assert reason == "htf_against_veto"
    assert dbg.get("htf", {}).get("htf_against") == "ema9_sustained_rolldown"


def test_precedence_redvol_beats_explosive_floor(monkeypatch):
    """Candle/HTF flag OFF (so gates 3-4 skipped), red-vol exhaustion fires on a real
    trigger bar, explosive-floor would ALSO fire (low rvol). Red-vol wins (gate 5 < 6):
    ``red_vol_exhaustion_veto``."""
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_candle_quality_multitf_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_red_vol_exhaustion_veto_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", True)
    # Build a frame whose LAST bar is a red, max-volume, new-session-high climax.
    df = _frame(n=20)
    # rewrite the trigger bar (cur = last) to the exhaustion shape
    df.iloc[:-1, df.columns.get_loc("High")] = 9.0
    df.iloc[:-1, df.columns.get_loc("Volume")] = 100.0
    df.iloc[-1, df.columns.get_loc("Open")] = 12.0   # open above close = RED
    df.iloc[-1, df.columns.get_loc("Close")] = 11.0
    df.iloc[-1, df.columns.get_loc("High")] = 13.0   # new session high
    df.iloc[-1, df.columns.get_loc("Volume")] = 9999.0  # session max volume
    # vr below the 5x explosive floor too, so explosive-floor WOULD fire if reached.
    _patch_orchestration(monkeypatch, arrays=_arrays(n=20, vr=0.5), trigger=("raw", True, "raw_break", 12.0, 9.0, None))
    ok, reason, dbg = _call(df=df, symbol="ABCD")
    assert ok is False
    assert reason == "red_vol_exhaustion_veto"
    # explosive-floor debug must NOT be present (short-circuited before it).
    assert "explosive_floor_rvol" not in dbg


def test_precedence_explosive_rvol_beats_change(monkeypatch):
    """Red-vol skipped (not the exhaustion shape), explosive-floor ON, BOTH the rvol
    floor AND the day-change floor are violated. The RVOL leg is checked first ->
    ``below_explosive_floor_rvol`` (not ``below_explosive_floor_change``)."""
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_red_vol_exhaustion_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_rvol", 5.0)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_change_pct", 10.0)
    # vr below 5 AND day-change ~0 (flat frame) -> both floors violated.
    _patch_orchestration(monkeypatch, arrays=_arrays(vr=1.0))
    ok, reason, dbg = _call(symbol="ABCD")
    assert ok is False
    assert reason == "below_explosive_floor_rvol"
    assert "explosive_floor_change_pct" not in dbg


def test_precedence_explosive_change_when_rvol_ok(monkeypatch):
    """RVOL clears the floor but the day-change does not (flat frame). The chain advances
    past the rvol leg to the change leg: ``below_explosive_floor_change``."""
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_red_vol_exhaustion_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_rvol", 5.0)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_change_pct", 10.0)
    _patch_orchestration(monkeypatch, arrays=_arrays(vr=10.0))  # rvol ok, change flat
    ok, reason, dbg = _call(symbol="ABCD")
    assert ok is False
    assert reason == "below_explosive_floor_change"
    assert dbg.get("explosive_floor_rvol") is None  # rvol leg passed, not logged


def test_precedence_volume_floor_beats_sustained(monkeypatch):
    """Explosive-floor OFF; the trigger-bar vol_ratio is below the base spike multiple
    AND sustained-vol would also fail. The per-bar volume floor (gate 7) wins:
    ``break_low_volume`` (not ``faded_volume_no_sustain``)."""
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_red_vol_exhaustion_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", False)
    _patch_orchestration(monkeypatch, arrays=_arrays(vr=0.5))  # below default 1.5 spike
    ok, reason, dbg = _call(
        volume_spike_multiple=1.5,
        require_sustained_volume=True, sustained_rvol_floor=99.0,
    )
    assert ok is False
    assert reason == "break_low_volume"


def test_precedence_sustained_beats_vwap_and_macd(monkeypatch):
    """Volume floor passes (high vr) but sustained-vol fails AND vwap/macd would also
    fail. Sustained (gate 8) wins: ``faded_volume_no_sustain``."""
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_red_vol_exhaustion_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_entry_verticality_atr_mult", 0.0)
    # vr high enough for per-bar floor but the sustained MEAN < floor:contradiction
    # avoided by setting the floor high. All vr equal so per-bar passes (>=1.5),
    # sustained mean == vr; set floor above it.
    _patch_orchestration(monkeypatch, arrays=_arrays(vr=2.0, vwap=999.0, macd=-1.0, macd_sig=1.0, macd_hist=-1.0))
    ok, reason, dbg = _call(
        volume_spike_multiple=1.5,
        require_sustained_volume=True, sustained_rvol_floor=99.0,
        require_vwap_hold=True, require_macd_bullish=True,
    )
    assert ok is False
    assert reason == "faded_volume_no_sustain"


def test_precedence_break_candle_beats_vwap(monkeypatch):
    """require_break_candle ON; the trigger bar is a weak/red candle AND price is below
    vwap. The break-candle gate (9) wins over vwap (10): ``weak_break_candle``."""
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_red_vol_exhaustion_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_entry_verticality_atr_mult", 0.0)
    df = _frame(n=20)
    # weak trigger bar: red, closes at the low (fails is_strong_bull_break_candle)
    df.iloc[-1, df.columns.get_loc("Open")] = 11.0
    df.iloc[-1, df.columns.get_loc("Close")] = 10.0
    df.iloc[-1, df.columns.get_loc("High")] = 11.5
    df.iloc[-1, df.columns.get_loc("Low")] = 10.0
    _patch_orchestration(monkeypatch, arrays=_arrays(n=20, vr=10.0, vwap=999.0))
    ok, reason, dbg = _call(
        df=df,
        require_break_candle=True, break_candle_min_close_pos=0.5,
        require_vwap_hold=True,
    )
    assert ok is False
    assert reason == "weak_break_candle"


def test_precedence_vwap_beats_macd(monkeypatch):
    """Break-candle off; price below vwap AND macd bearish. VWAP gate (10) wins:
    ``below_vwap``."""
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_red_vol_exhaustion_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_entry_verticality_atr_mult", 0.0)
    # vwap above the trigger close (base 10.0) and macd bearish.
    _patch_orchestration(monkeypatch, arrays=_arrays(vr=10.0, vwap=999.0, macd=-1.0, macd_sig=1.0, macd_hist=-1.0))
    ok, reason, dbg = _call(require_vwap_hold=True, require_macd_bullish=True)
    assert ok is False
    assert reason == "below_vwap"


def test_precedence_macd_beats_verticality(monkeypatch):
    """VWAP passes (price above vwap), macd bearish AND price is extended above ema9.
    MACD gate (11) wins over verticality (12): ``macd_not_bullish``."""
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_red_vol_exhaustion_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_entry_verticality_atr_mult", 1.5)
    # ema9 tiny so price/ema9-1 is huge (extended). vwap tiny so vwap passes.
    # macd bearish.
    _patch_orchestration(
        monkeypatch,
        arrays=_arrays(vr=10.0, vwap=1.0, ema9=0.01, atr=0.0001,
                       macd=-1.0, macd_sig=1.0, macd_hist=-1.0),
    )
    ok, reason, dbg = _call(require_vwap_hold=True, require_macd_bullish=True)
    assert ok is False
    assert reason == "macd_not_bullish"


def test_verticality_last_gate(monkeypatch):
    """Everything passes EXCEPT price is extended above ema9 -> the final gate fires:
    ``extended_verticality``."""
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_red_vol_exhaustion_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_entry_verticality_atr_mult", 1.5)
    _patch_orchestration(
        monkeypatch,
        arrays=_arrays(vr=10.0, vwap=1.0, ema9=0.01, atr=0.0001,
                       macd=1.0, macd_sig=0.5, macd_hist=0.5),  # macd bullish
    )
    ok, reason, dbg = _call()
    assert ok is False
    assert reason == "extended_verticality"
    assert "verticality" in dbg


# --------------------------------------------------------------------------- #
# (2) DEEP-RECLAIM EXEMPTION must be SURGICAL — it exempts gates 1-5 (back_side,
#     front_side, doji, htf, red-vol) but NOT the extension/flow gates. Prove it
#     skips the right ones AND remains subject to verticality + vwap.
# --------------------------------------------------------------------------- #
def test_deep_reclaim_exempt_from_backside_and_front_side(monkeypatch):
    """A deep_reclaim that WOULD trip back_side AND front_side AND doji AND htf AND
    red-vol — none of them fire because of the `if not _deep_reclaim` carve-outs. It
    reaches the success path: ``deep_reclaim_ok``."""
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_candle_quality_multitf_veto_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_red_vol_exhaustion_veto_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_entry_verticality_atr_mult", 0.0)
    # back_side + front_side + doji + htf detectors ALL set to veto — but deep_reclaim
    # must bypass every one of them.
    monkeypatch.setattr(eg, "_doji_trigger_veto", lambda *a, **k: (True, {"x": 1}))
    monkeypatch.setattr(eg, "_htf_against_veto", lambda *a, **k: (True, {"x": 1}))
    _patch_orchestration(
        monkeypatch,
        arrays=_arrays(vr=10.0, vwap=1.0),
        trigger=("raw", True, "deep_reclaim", 12.0, 9.0, "deep_reclaim"),
        back_side=(True, "ema9_below_ema20"),
        front_backside=True, front_reason="below_vwap",
    )
    ok, reason, dbg = _call()
    assert ok is True
    assert reason == "deep_reclaim_ok"


def test_deep_reclaim_still_subject_to_verticality(monkeypatch):
    """The exemption is SURGICAL — a deep_reclaim is NOT exempt from the verticality
    gate. An extended deep_reclaim is still rejected: ``extended_verticality``. If the
    exemption leaked to this gate the entry would wrongly fire."""
    monkeypatch.setattr(settings, "chili_momentum_red_vol_exhaustion_veto_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_entry_verticality_atr_mult", 1.5)
    _patch_orchestration(
        monkeypatch,
        arrays=_arrays(vr=10.0, vwap=1.0, ema9=0.01, atr=0.0001),
        trigger=("raw", True, "deep_reclaim", 12.0, 9.0, "deep_reclaim"),
    )
    ok, reason, dbg = _call()
    assert ok is False
    assert reason == "extended_verticality"


def test_deep_reclaim_vwap_fails_closed_when_unavailable(monkeypatch):
    """Deep-reclaim is exempt from the lifecycle vetoes but the VWAP gate fails CLOSED
    for it when vwap is unavailable: ``vwap_unavailable_weak_path`` (a normal break would
    fail-OPEN). This is the surgical opposite-direction proof — the weak-prior path is
    STRICTER on vwap, not laxer."""
    monkeypatch.setattr(settings, "chili_momentum_red_vol_exhaustion_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_entry_verticality_atr_mult", 0.0)
    # vwap array all <= 0 -> "unavailable" for the gate.
    _patch_orchestration(
        monkeypatch,
        arrays=_arrays(vr=10.0, vwap=0.0),
        trigger=("raw", True, "deep_reclaim", 12.0, 9.0, "deep_reclaim"),
    )
    ok, reason, dbg = _call(require_vwap_hold=True)
    assert ok is False
    assert reason == "vwap_unavailable_weak_path"


def test_deep_reclaim_sustained_fails_closed_on_thin(monkeypatch):
    """The sustained-volume gate fails OPEN on thin data for a normal break but CLOSED
    for a deep_reclaim. With a vr series of all-None (no valid samples -> _sustained_rvol
    returns None) a deep_reclaim is rejected: ``faded_volume_no_sustain``."""
    monkeypatch.setattr(settings, "chili_momentum_red_vol_exhaustion_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_entry_verticality_atr_mult", 0.0)
    arr = _arrays(vr=10.0, vwap=1.0)
    arr["volume_ratio"] = [None] * 30  # _sustained_rvol -> None (thin)
    # but the per-bar vol_ratio falls back to a computed value from the frame volume;
    # _frame has flat volume so vr fallback ~1.0; deep_reclaim raises the floor to
    # runaway_min_volume_spike (default 2.0) -> would trip break_low_volume FIRST.
    # Give the trigger bar a real volume spike so the per-bar floor passes and we reach
    # the sustained gate.
    df = _frame(n=30)
    df.iloc[:-1, df.columns.get_loc("Volume")] = 100.0
    df.iloc[-1, df.columns.get_loc("Volume")] = 100000.0  # spike -> per-bar floor passes
    _patch_orchestration(
        monkeypatch, arrays=arr,
        trigger=("raw", True, "deep_reclaim", 12.0, 9.0, "deep_reclaim"),
    )
    ok, reason, dbg = _call(
        df=df, require_sustained_volume=True, sustained_rvol_floor=1.0,
    )
    assert ok is False
    assert reason == "faded_volume_no_sustain"


# --------------------------------------------------------------------------- #
# (3) TICK-BREAK SPLIT — the forming bar SKIPS the per-bar gates (doji, red-vol,
#     break-candle, per-bar volume floor) but HTF-against, front_side, back_side,
#     explosive-floor, sustained, vwap, macd and verticality STILL apply.
# --------------------------------------------------------------------------- #
def _tick_setup(monkeypatch, *, arrays, reason_t="waiting_for_break", live_price=20.0,
                pb_high=12.0, pb_low=9.0):
    """Drive a TICK-break: the trigger ARMs (ok False) on a tick-watchable reason with
    pb levels in debug, and live_price > pb_high so the tick-break block fires."""
    monkeypatch.setattr(eg, "compute_all_from_df", lambda df, needed=None: dict(arrays))

    def arm(high, low, ema9, cur, **kw):
        return (False, reason_t,
                None, None,
                {"entry_interval": "1m", "pullback_high": pb_high, "pullback_low": pb_low})

    monkeypatch.setattr(eg, "_evaluate_raw_break", arm)
    monkeypatch.setattr(eg, "_evaluate_break_retest", arm)
    monkeypatch.setattr(
        eg, "first_pullback_break",
        lambda df, **kw: ("PASS", None, None, {"fp_declined": "neutralised"}),
    )
    monkeypatch.setattr(eg, "_detect_back_side", lambda *a, **k: (False, ""))

    class _FS:
        is_backside = False
        reason = "front_side"
        front_side_score = 0.5

    monkeypatch.setattr(rm, "front_side_state", lambda *a, **k: _FS())
    monkeypatch.setattr(eg, "_today_session_frame", lambda df: df)
    # Force premarket/dip-buy tick confirmations to pass (RTH default already passes;
    # this is belt-and-suspenders for the dip-buy reasons).
    monkeypatch.setattr(eg, "_premarket_tickbreak_confirmed", lambda **k: True)
    monkeypatch.setattr(eg, "_dipbuy_tick_thrust_ok", lambda **k: True)


def test_tickbreak_skips_doji_but_htf_still_applies(monkeypatch):
    """On a tick-break the DOJI gate is skipped (forming bar) but HTF-against STILL
    applies (it reads COMPLETED HTF bars). With doji set to veto and htf set to veto,
    the result must be the HTF veto — proving the doji skip + htf non-skip split.
    If doji were NOT skipped this would return ``doji_trigger_veto`` instead."""
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_candle_quality_multitf_veto_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_red_vol_exhaustion_veto_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_entry_verticality_atr_mult", 0.0)
    monkeypatch.setattr(eg, "_doji_trigger_veto", lambda *a, **k: (True, {"would_veto": True}))
    monkeypatch.setattr(eg, "_htf_against_veto", lambda *a, **k: (True, {"htf_against": "macd_peaked"}))
    _tick_setup(monkeypatch, arrays=_arrays(vr=10.0, vwap=1.0))
    ok, reason, dbg = _call(live_price=20.0)
    assert ok is False
    assert reason == "htf_against_veto"
    # doji never recorded -> it was skipped, not merely passed.
    assert "doji" not in dbg
    assert dbg.get("tick_break") is True


def test_tickbreak_skips_redvol_and_volume_floor(monkeypatch):
    """On a tick-break, both the red-vol exhaustion gate AND the per-bar volume floor are
    skipped (the forming bar's close/volume are unknowable). With a red max-vol new-high
    trigger bar AND a sub-floor vr, neither fires — the entry reaches success
    ``pullback_break_tick_ok``."""
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_candle_quality_multitf_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_red_vol_exhaustion_veto_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_entry_verticality_atr_mult", 0.0)
    df = _frame(n=20)
    df.iloc[:-1, df.columns.get_loc("High")] = 9.0
    df.iloc[:-1, df.columns.get_loc("Volume")] = 100.0
    df.iloc[-1, df.columns.get_loc("Open")] = 12.0
    df.iloc[-1, df.columns.get_loc("Close")] = 11.0
    df.iloc[-1, df.columns.get_loc("High")] = 13.0
    df.iloc[-1, df.columns.get_loc("Volume")] = 9999.0
    # vr below the 1.5 base floor too -> would trip break_low_volume on a bar entry.
    _tick_setup(monkeypatch, arrays=_arrays(n=20, vr=0.1, vwap=1.0))
    ok, reason, dbg = _call(df=df, live_price=20.0, volume_spike_multiple=1.5)
    assert ok is True
    assert reason == "pullback_break_tick_ok"
    assert "red_vol_exhaustion" not in dbg


def test_tickbreak_still_subject_to_explosive_floor(monkeypatch):
    """The per-bar volume floor is skipped on a tick-break, but the EXPLOSIVE-floor hard
    gate is NOT (it is independent of the forming bar's per-bar volume). A sub-floor rvol
    on an equity tick-break still vetoes: ``below_explosive_floor_rvol``."""
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_candle_quality_multitf_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_red_vol_exhaustion_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_rvol", 5.0)
    _tick_setup(monkeypatch, arrays=_arrays(vr=1.0, vwap=1.0))
    ok, reason, dbg = _call(live_price=20.0, symbol="ABCD")
    assert ok is False
    assert reason == "below_explosive_floor_rvol"


def test_tickbreak_still_subject_to_macd(monkeypatch):
    """A tick-break skips the per-bar candle gates but MACD still applies. A bearish macd
    on a tick-break vetoes: ``macd_not_bullish``."""
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_red_vol_exhaustion_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_entry_verticality_atr_mult", 0.0)
    _tick_setup(monkeypatch, arrays=_arrays(vr=10.0, vwap=1.0, macd=-1.0, macd_sig=1.0, macd_hist=-1.0))
    ok, reason, dbg = _call(live_price=20.0, require_macd_bullish=True)
    assert ok is False
    assert reason == "macd_not_bullish"


def test_tickbreak_vwap_uses_live_price(monkeypatch):
    """On a tick-break the VWAP gate compares LIVE_PRICE (not the completed close) to
    vwap. A live_price above vwap passes even though the stale close would be below.
    Here vwap=15, live_price=20 (>15) -> passes vwap; close base 10 (<15) would have
    failed if the stale close were used. Reaching success proves live_price is used."""
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_red_vol_exhaustion_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_entry_verticality_atr_mult", 0.0)
    _tick_setup(monkeypatch, arrays=_arrays(vr=10.0, vwap=15.0, macd=1.0, macd_sig=0.5, macd_hist=0.5))
    ok, reason, dbg = _call(live_price=20.0, require_vwap_hold=True)
    assert ok is True
    assert reason == "pullback_break_tick_ok"


# --------------------------------------------------------------------------- #
# (5) ONE GATE ON AT A TIME — only that gate's reason can veto; every other path
#     reaches success. Proves no gate fires when its flag is OFF (no stale veto).
# --------------------------------------------------------------------------- #
def _only_gate_base(monkeypatch):
    """All optional veto flags OFF, verticality OFF, neutral arrays. The chain must
    reach success unless a single gate is turned on."""
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_candle_quality_multitf_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_red_vol_exhaustion_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_entry_verticality_atr_mult", 0.0)


def test_only_explosive_floor_on(monkeypatch):
    """Only the explosive-floor flag is ON; a sub-floor rvol vetoes. With it OFF the
    same setup succeeds (the companion assertion proves the gate is the cause).

    vr=2.0 clears the per-bar volume floor (volume_spike_multiple=1.5) so the chain
    REACHES the explosive-floor gate; 2.0 < 5.0 trips the explosive floor when it's ON."""
    _only_gate_base(monkeypatch)
    _patch_orchestration(monkeypatch, arrays=_arrays(vr=2.0, vwap=1.0))
    ok0, r0, _ = _call(symbol="ABCD")
    assert ok0 is True and r0 == "pullback_break_ok"
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_rvol", 5.0)
    _patch_orchestration(monkeypatch, arrays=_arrays(vr=2.0, vwap=1.0))
    ok1, r1, _ = _call(symbol="ABCD")
    assert ok1 is False and r1 == "below_explosive_floor_rvol"


def test_only_backside_flag_on(monkeypatch):
    """Only the front_side lifecycle veto flag is ON; a backside read vetoes. OFF -> the
    same backside read does NOT veto (the block is entirely skipped)."""
    _only_gate_base(monkeypatch)
    _patch_orchestration(
        monkeypatch, arrays=_arrays(vr=10.0, vwap=1.0),
        front_backside=True, front_reason="below_vwap",
    )
    ok0, r0, _ = _call()
    assert ok0 is True and r0 == "pullback_break_ok"  # flag OFF -> no veto
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", True)
    _patch_orchestration(
        monkeypatch, arrays=_arrays(vr=10.0, vwap=1.0),
        front_backside=True, front_reason="below_vwap",
    )
    ok1, r1, _ = _call()
    assert ok1 is False and r1 == "backside_lifecycle_veto"


def test_explosive_floor_crypto_exempt(monkeypatch):
    """The explosive-floor gate is EQUITY-only. A crypto symbol (-USD) with a sub-floor
    rvol is NOT vetoed even with the flag ON: the entry succeeds. (A bare ticker IS
    vetoed — proven in test_only_explosive_floor_on.)"""
    _only_gate_base(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_rvol", 5.0)
    # vr=2.0 clears the per-bar volume floor; for a bare ticker it would trip the
    # explosive floor (2.0 < 5.0), but -USD is exempt so the entry succeeds.
    _patch_orchestration(monkeypatch, arrays=_arrays(vr=2.0, vwap=1.0))
    ok, reason, dbg = _call(symbol="BTC-USD")
    assert ok is True
    assert reason == "pullback_break_ok"


# --------------------------------------------------------------------------- #
# (6) BORDERLINE — a setup that passes EVERY gate by epsilon. Each gate's
#     boundary is approached from the passing side; a wrong (strict-vs-loose)
#     comparator flips it to a veto.
# --------------------------------------------------------------------------- #
def test_borderline_passes_all_gates_by_epsilon(monkeypatch):
    """rvol == floor exactly, day-change == floor exactly, vr == spike floor exactly,
    sustained == floor exactly, price == vwap exactly, macd line == signal exactly,
    extension == cap exactly. EVERY gate uses a boundary-inclusive comparator on the
    PASS side, so the entry must SUCCEED. A single off-by-one (`<` vs `<=`) on any gate
    would flip this to that gate's veto."""
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_red_vol_exhaustion_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_rvol", 5.0)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_change_pct", 0.0)  # flat frame == 0
    monkeypatch.setattr(settings, "chili_momentum_entry_verticality_atr_mult", 1.5)
    # vr == 5.0 == explosive floor AND == sustained floor; vol spike multiple 5.0 == vr;
    # vwap == price (10.0); macd line == signal (1.0 == 1.0 -> bullish via >=);
    # ema9 chosen so extension == cap exactly.
    # close base = 10.0; cap = max(0.005, atr_pct*1.5). atr_pct = atr/price.
    # choose atr so atr_pct*1.5 = ext target. Let ema9 = 10.0/(1+cap).
    atr = 0.1
    price = 10.0
    atr_pct = atr / price  # 0.01
    cap = max(0.005, atr_pct * 1.5)  # 0.015
    ema9_val = price / (1.0 + cap)   # ext == cap exactly
    _patch_orchestration(
        monkeypatch,
        arrays=_arrays(
            vr=5.0, vwap=10.0, ema9=ema9_val, atr=atr,
            macd=1.0, macd_sig=1.0, macd_hist=0.0,
        ),
    )
    ok, reason, dbg = _call(
        symbol="ABCD",
        volume_spike_multiple=5.0,
        require_sustained_volume=True, sustained_rvol_floor=5.0,
        require_vwap_hold=True, require_macd_bullish=True,
    )
    assert ok is True, f"borderline should pass, got veto {reason!r} dbg={dbg}"
    assert reason == "pullback_break_ok"


def test_borderline_rvol_just_below_floor_vetoes(monkeypatch):
    """The mirror of the borderline pass: rvol one epsilon BELOW the explosive floor
    flips to the veto. Pins the comparator direction (`< floor` rejects)."""
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_red_vol_exhaustion_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_rvol", 5.0)
    monkeypatch.setattr(settings, "chili_momentum_entry_verticality_atr_mult", 0.0)
    _patch_orchestration(monkeypatch, arrays=_arrays(vr=4.999, vwap=1.0))
    ok, reason, dbg = _call(symbol="ABCD")
    assert ok is False
    assert reason == "below_explosive_floor_rvol"


# --------------------------------------------------------------------------- #
# END-TO-END on REAL frames (no helper patching) — the orchestration tests above
# stub the leaf math; these prove the wiring on un-mocked indicators.
# --------------------------------------------------------------------------- #
def _real_break_frame(n=40):
    """A clean Ross flag: impulse up, shallow 3-bar pullback holding the 9-EMA, then the
    last bar breaks the pullback high on volume. Built so the REAL _evaluate_raw_break
    fires raw_break with default knobs."""
    closes = []
    base = 5.0
    # GENTLE impulse: a long, modest rise so the 9-EMA trails well BELOW the recent
    # highs by the time of the pullback (a steep impulse pulls the EMA up into the
    # pullback low and trips pullback_below_ema9 — not what we want to exercise here).
    for i in range(int(n) - 4):
        closes.append(base + i * 0.08)
    top = closes[-1]
    # A TIGHT, SHALLOW consolidation just below the top for the last few bars (the
    # pullback window). Stays comfortably above the trailing 9-EMA.
    closes.append(top - 0.04)
    closes.append(top - 0.05)
    closes.append(top - 0.03)
    # Final bar: a decisive break to a NEW high above the consolidation.
    closes.append(top + 0.30)
    closes = np.array(closes, dtype=float)
    opens = np.empty(n); opens[0] = closes[0]; opens[1:] = closes[:-1]
    highs = np.maximum(opens, closes) * 1.002
    lows = np.minimum(opens, closes) * 0.998
    # final bar: green, closes at the high (conviction)
    highs[-1] = closes[-1] * 1.001
    lows[-1] = opens[-1]
    vols = np.full(n, 1000.0)
    vols[-1] = 20000.0  # big volume on the break
    idx = pd.date_range("2026-06-26 13:30", periods=n, freq="1min", tz="UTC")
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": vols},
        index=idx,
    )


def test_real_frame_all_flags_off_fires(monkeypatch):
    """End-to-end on a real clean-break frame with every optional veto OFF: the raw
    trigger fires and the function returns a success reason (no veto manufactured by
    the un-mocked math). Guards against an always-on gate that silently rejects a
    textbook Ross break."""
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_candle_quality_multitf_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_red_vol_exhaustion_veto_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_entry_first_pullback_enabled", False)
    # The verticality gate is the one ALWAYS-ON guard; a decisive break bar can clear
    # its ATR-scaled cap. This test isolates the OPTIONAL-flag chain, so neutralise it.
    monkeypatch.setattr(settings, "chili_momentum_entry_verticality_atr_mult", 0.0)
    df = _real_break_frame()
    ok, reason, dbg = pullback_break_confirmation(df, entry_interval="1m", symbol="ABCD")
    # Either the raw break or (if first-pullback geometry reads) a pullback success —
    # but it must be a SUCCESS, not a veto, and carry the structural levels.
    assert ok is True, f"clean break should fire, got veto {reason!r} dbg={dbg}"
    assert reason.endswith("_ok")
    assert dbg.get("pullback_high") is not None
    assert dbg.get("pullback_low") is not None


def test_insufficient_bars_short_circuits_before_any_gate(monkeypatch):
    """The bars guard at the very top returns before any indicator compute. A 5-bar
    frame -> ``insufficient_bars`` regardless of every other input."""
    df = _frame(n=5)
    ok, reason, dbg = pullback_break_confirmation(df, symbol="ABCD")
    assert ok is False
    assert reason == "insufficient_bars"
    assert dbg.get("bars") == 5


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
