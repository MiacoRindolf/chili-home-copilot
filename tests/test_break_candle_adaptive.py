"""ADAPTIVE break-candle close-position floor — the one entry-side gate fix from the
gate audit (the FIXED 0.50 ``break_candle_min_close_pos`` over-rejected explosive FIRST
pushes: 53%% of ``weak_break_candle`` blocks ran +3%%; SKYQ/FRTT/WEN/HELP/BOLD ran but
never filled). Ross buys the first STRONG push even when the 1m candle is not textbook.

The fix (entry_gates.py ``pullback_break_confirmation`` ~ the ``weak_break_candle`` gate):
when ``chili_momentum_break_candle_adaptive_close_pos_enabled`` is ON **and** the trigger-bar
RVOL is explosive (>= the SAME ``chili_momentum_explosive_floor_rvol`` the E3 hard floor
already trusts), the close-position requirement floats DOWN from the textbook 0.50 toward
``chili_momentum_break_candle_adaptive_close_pos_floor`` (the ONE documented relaxed-floor
base), scaled by how far RVOL exceeds the floor (RVOL-DERIVED, no new magic threshold). For
a green break bar the upper-wick fraction is identically ``1 - close_pos``, so the gate's
wick cap is floated up in lockstep; otherwise the wick check would silently re-impose the
exact 0.50 floor we are relaxing.

DEFAULT OFF (this is a skip-tick gate whose evidence is confounded by the whole stack — it
must be A/B'd on the recorded-fills replay first). Flag OFF ⇒ byte-identical to the fixed
0.50 gate.

These tests drive the REAL ``pullback_break_confirmation`` confirmation ladder (the function
``momentum_pullback_trigger`` / ``live_runner.py`` call). Only ``require_break_candle`` is on
so the trigger bar's close-position + RVOL are the controlled variables; the verticality cap
is set to 0 (OFF live) so a clean break reaches the candle gate; a ``-USD`` symbol exempts the
equity E3 explosive-floor day-change gate so RVOL alone drives the relaxation. ``db=None`` ⇒
the L2 / signed-tape confirmers fail open. TESTS-ONLY — never edits source.

  TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test \\
    conda run -n chili-env python -m pytest tests/test_break_candle_adaptive.py -q
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.services.trading.momentum_neural import entry_gates as eg
from app.services.trading.momentum_neural.entry_gates import pullback_break_confirmation

_OPEN = "2026-06-26 13:30"


def _ohlcv(bars: list[tuple], start: str = _OPEN) -> pd.DataFrame:
    df = pd.DataFrame(
        [{"Open": o, "High": h, "Low": l, "Close": c, "Volume": v} for o, h, l, c, v in bars]
    )
    df.index = pd.date_range(start, periods=len(df), freq="1min", tz="UTC")
    return df


def _warm(px0: float, n: int = 12, step: float = 0.03, vol: int = 2_000_000):
    out, px = [], px0
    for _ in range(n):
        o = px
        c = round(px + step, 2)
        h = round(c + 0.02, 2)
        l = round(o - 0.02, 2)
        out.append((round(o, 2), h, l, c, vol))
        px = c
    return out, px


def _cons(px: float, n: int = 8, vol: int = 1_400_000) -> list[tuple]:
    out = []
    for i in range(n):
        o = px
        c = round(px + (0.01 if i % 2 else 0.0), 2)
        h = round(max(o, c) + 0.03, 2)
        l = round(min(o, c) - 0.03, 2)
        out.append((round(o, 2), h, l, round(c, 2), vol))
    return out


def _frame(trigger_close_pos: float, trigger_vol: int) -> pd.DataFrame:
    """A warmed first-pullback frame whose LAST (trigger) bar breaks the prior swing high
    with a CONTROLLED close-position within its own range and a CONTROLLED volume (which
    drives the trigger-bar RVOL). The break bar is GREEN (close > open), so the only thing
    deciding the candle gate is its close-position vs the (possibly relaxed) floor."""
    w, _ = _warm(2.70, 12, 0.03)
    base = w + _cons(3.06) + [
        (3.10, 3.24, 3.09, 3.22, 5_000_000),
        (3.22, 3.30, 3.21, 3.28, 6_000_000),
        (3.28, 3.30, 3.23, 3.25, 1_800_000),   # shallow pull
        (3.25, 3.28, 3.22, 3.26, 1_500_000),
    ]
    # Trigger bar: low 3.27, high 3.40 (breaks the ~3.30 swing high), open 3.28 (green),
    # close placed precisely at trigger_close_pos of [low, high].
    lo, hi, op = 3.27, 3.40, 3.28
    cl = round(lo + trigger_close_pos * (hi - lo), 4)
    base.append((op, hi, lo, cl, trigger_vol))
    return _ohlcv(base)


# Only the break-candle gate is active; the trigger bar's close-pos + RVOL are the variables.
_KW = dict(
    require_retest=False,
    require_sustained_volume=False,
    require_break_candle=True,
    require_vwap_hold=False,
    require_macd_bullish=False,
    volume_spike_multiple=1.0,
    break_candle_min_close_pos=0.50,
)


def _drive(close_pos: float, vol: int, *, adaptive: bool, symbol: str = "TEST-USD") -> dict:
    """Drive the REAL pullback confirmation ladder with the adaptive flag set; restore the
    flags afterwards. Verticality OFF (its live binding) so a clean break reaches the gate."""
    saved_vert = getattr(eg.settings, "chili_momentum_entry_verticality_atr_mult", 1.5)
    saved_flag = getattr(eg.settings, "chili_momentum_break_candle_adaptive_close_pos_enabled", False)
    eg.settings.chili_momentum_entry_verticality_atr_mult = 0.0
    eg.settings.chili_momentum_break_candle_adaptive_close_pos_enabled = adaptive
    try:
        ok, reason, dbg = pullback_break_confirmation(
            _frame(close_pos, vol), entry_interval="1m", symbol=symbol, db=None, **_KW
        )
    finally:
        eg.settings.chili_momentum_entry_verticality_atr_mult = saved_vert
        eg.settings.chili_momentum_break_candle_adaptive_close_pos_enabled = saved_flag
    return {"ok": bool(ok), "reason": reason, "rvol": dbg.get("vol_ratio"),
            "adaptive": dbg.get("break_candle_adaptive_close_pos")}


# RVOL bands (warmed-frame avg vol ≈ 2.07M, so RVOL ≈ trigger_vol / 2.07M):
#   _VOL_HI  -> RVOL ~12x  (>= 2x the 5x floor -> floor decays fully to the relaxed 0.30)
#   _VOL_MID -> RVOL ~6.7x (1.34x the floor    -> floor partially relaxed, ~0.43)
#   _VOL_ORD -> RVOL ~1.4x (BELOW the 5x floor -> NO relaxation; textbook 0.50 holds)
_VOL_HI = 60_000_000
_VOL_MID = 20_000_000
_VOL_ORD = 3_000_000


def _floor() -> float:
    return float(getattr(eg.settings, "chili_momentum_explosive_floor_rvol", 5.0))


# ── (a) a HIGH-RVOL break with close-pos between the relaxed floor and 0.50: blocked when
#        the flag is OFF, PASSES when the flag is ON. ────────────────────────────────────
def test_a_high_rvol_relaxes_close_pos_passes_when_on() -> None:
    # close-pos 0.40 sits between the relaxed floor (0.30) and the textbook 0.50.
    off = _drive(0.40, _VOL_HI, adaptive=False)
    assert off["rvol"] is not None and off["rvol"] >= _floor(), "fixture must be explosive"
    assert off["ok"] is False and off["reason"] == "weak_break_candle", (
        f"flag OFF must keep the textbook 0.50 gate (blocks a 0.40-close break): {off}"
    )

    on = _drive(0.40, _VOL_HI, adaptive=True)
    assert on["ok"] is True, f"flag ON must relax the explosive break and fire: {on}"
    assert on["adaptive"] is not None, "the relaxed path must be recorded in debug"
    # The relaxation is RVOL-DERIVED: a high RVOL (>= 2x floor) decays the floor toward 0.30.
    assert on["adaptive"]["effective_min_close_pos"] <= 0.40 + 1e-9


def test_a_mid_rvol_partial_relaxation_is_rvol_derived() -> None:
    # A mid RVOL (~1.34x the floor) only PARTIALLY relaxes: the floor lands between 0.30 and
    # 0.50 (here ~0.43), so a 0.40-close break is STILL just below it and still blocks — proof
    # the relaxation tracks RVOL rather than snapping to the floor.
    mid = _drive(0.40, _VOL_MID, adaptive=True)
    assert mid["rvol"] is not None and mid["rvol"] >= _floor()
    eff = mid["adaptive"]["effective_min_close_pos"]
    assert 0.30 < eff < 0.50, f"mid RVOL must partially relax, not snap to the floor: {eff}"
    assert mid["ok"] is False and mid["reason"] == "weak_break_candle", (
        f"0.40 close is below the partially-relaxed floor {eff} -> still blocks: {mid}"
    )
    # And a close-pos ABOVE that partially-relaxed floor fires.
    above = _drive(eff + 0.03, _VOL_MID, adaptive=True)
    assert above["ok"] is True, f"a close above the partially-relaxed floor must fire: {above}"


# ── (b) an ORDINARY-RVOL weak candle still BLOCKS (relaxation does NOT apply to non-explosive
#        tape). ─────────────────────────────────────────────────────────────────────────
def test_b_ordinary_rvol_weak_candle_still_blocks() -> None:
    ord_on = _drive(0.40, _VOL_ORD, adaptive=True)
    assert ord_on["rvol"] is not None and ord_on["rvol"] < _floor(), "fixture must be non-explosive"
    assert ord_on["adaptive"] is None, "relaxation must NOT engage below the explosive floor"
    assert ord_on["ok"] is False and ord_on["reason"] == "weak_break_candle", (
        f"a weak break on ordinary volume must still block at the textbook 0.50: {ord_on}"
    )


# ── (c) a genuinely weak / doji break still BLOCKS even at high RVOL (below the relaxed
#        floor) — doji rejection preserved. ─────────────────────────────────────────────
def test_c_doji_below_relaxed_floor_still_blocks_at_high_rvol() -> None:
    relaxed_floor = float(
        getattr(eg.settings, "chili_momentum_break_candle_adaptive_close_pos_floor", 0.30)
    )
    # close-pos 0.15 is below the relaxed floor (0.30): a true doji/weak break.
    doji = _drive(relaxed_floor - 0.15, _VOL_HI, adaptive=True)
    assert doji["rvol"] is not None and doji["rvol"] >= _floor(), "fixture must be explosive"
    assert doji["ok"] is False and doji["reason"] == "weak_break_candle", (
        f"a doji below the relaxed floor must STILL block even on explosive volume: {doji}"
    )


# ── (d) flag-OFF is byte-identical to the fixed-0.50 gate (no relaxation, no debug key, same
#        verdict the textbook gate produces). ──────────────────────────────────────────────
def test_d_flag_off_byte_identical() -> None:
    # A strong textbook break (0.70) fires under BOTH flag states with the SAME reason; the
    # OFF path never records the adaptive debug key.
    for vol in (_VOL_HI, _VOL_MID, _VOL_ORD):
        off = _drive(0.70, vol, adaptive=False)
        assert off["adaptive"] is None, f"flag OFF must never relax / tag debug: {off}"

    # And for EVERY (close_pos, vol) combo the flag-OFF verdict matches the textbook gate:
    # block below 0.50, fire at/above 0.50 (subject to the other always-on structure checks).
    for close_pos in (0.20, 0.40, 0.49, 0.55, 0.70):
        for vol in (_VOL_ORD, _VOL_MID, _VOL_HI):
            off = _drive(close_pos, vol, adaptive=False)
            if close_pos < 0.50:
                # below the textbook floor -> the candle gate is the binding rejection
                assert off["reason"] in ("weak_break_candle", "break_low_volume"), off
            assert off["adaptive"] is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
