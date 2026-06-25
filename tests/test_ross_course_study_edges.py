"""Ross course-study edges E1/E2/E3 (docs/STRATEGY/CC_REPORTS/2026-06-24_ross-course-study.md).

Three ADDITIVE, flag-gated gates layered onto the momentum lane. Each test proves the
three required behaviours per edge:

  E1 — session-anchored BACKSIDE veto (entry_gates.pullback_break_confirmation): vetoes a
       backside (extended/faded) frame; NO-OPs on a genuine front-side frame; flag OFF ->
       byte-identical (no veto).
  E3 — EXPLOSIVE-FLOOR hard gate (entry_gates.pullback_break_confirmation): floors out a
       weak-batch low-RVOL / low-day-change name; ALLOWS a genuine explosive (RVOL>=5 AND
       change>=10%); flag OFF -> byte-identical (the floored name fires).
  E2 — CATALYST GRADING (catalyst.catalyst_grade_selection_delta): SUPPRESSES a weak
       (dilution/compliance/legal) catalyst (negative delta -> the caller drops live
       eligibility); BOOSTS a strong (FDA/M&A/contract) catalyst; NEUTRAL on a medium /
       absent headline; flag-equivalent OFF (absent feed) -> 0 (no change).

E1/E3 are exercised through the REAL entry gate on synthetic OHLCV frames (the public
contract); E2 is exercised through the pure selection-delta the viability block consumes
(the flag-gated viability wiring just adds this delta and, on a negative one, sets
live_eligible=False). Frames are tuned so each edge fires in isolation: the E3 frames hold
E1's veto OFF (independent flag) so the RVOL/change floor is the only variable under test,
mirroring the existing trigger-isolation pattern in test_pullback_break.py.
"""
from __future__ import annotations

import pandas as pd

from app.config import settings
from app.services.trading.momentum_neural.catalyst import (
    _catalyst_tilt,
    _is_strong_catalyst,
    catalyst_grade_selection_delta,
)
from app.services.trading.momentum_neural.entry_gates import pullback_break_confirmation
from app.services.trading.momentum_neural.ross_momentum import front_side_state


# ── frame builders ───────────────────────────────────────────────────────────

def _df(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [{"Open": c, "High": h, "Low": lo, "Close": c, "Volume": v} for (c, h, lo, v) in rows]
    )


def _base(close: float, vol: float = 1000.0) -> tuple[float, float, float, float]:
    return (close, close + 0.3, close - 0.3, vol)


def _isolate_verticality(monkeypatch) -> None:
    """Hold off the ATR-scaled verticality skip (added 2026-06-12, separately covered) so
    these synthetic impulses — which rise off a long flat base — reach the gate under test.
    Same rationale as test_pullback_break.py::_isolate_trigger_logic."""
    monkeypatch.setattr(settings, "chili_momentum_entry_verticality_atr_mult", 0.0, raising=False)


def _backside_chasing_top_rows() -> list[tuple[float, float, float, float]]:
    """The canonical shallow-pullback-then-break frame. Its last close sits at the very top
    of the day-range AND far above session VWAP (clean impulse off a flat base) -> a
    front_side_state BACKSIDE read (reason ``chasing_top``)."""
    rows = [_base(100.0) for _ in range(14)]
    rows += [_base(c) for c in (102.0, 104.0, 106.0, 108.0, 110.0)]  # impulse
    rows += [_base(109.0, 800.0), _base(108.5, 800.0)]               # shallow pullback
    rows.append((110.6, 111.2, 109.6, 3200.0))                       # break + volume spike
    return rows


def _front_side_explosive_rows() -> list[tuple[float, float, float, float]]:
    """A genuine FRONT-SIDE explosive: a choppy session that rises from the open (close-vs-
    VWAP dispersion is large -> dist_sigma < 2 -> NOT chasing_top), then a shallow pullback
    and break on heavy volume. Session open (first bar) 100 -> last close 112 (+12% >= 10%
    change floor); break vol_ratio ~7.3 (>= 5x RVOL floor). front_side_state -> NOT backside."""
    rows = [(100.0, 100.5, 99.5, 3000.0)]                            # session open bar
    for i in range(1, 18):
        base = 100.0 + i * 0.6
        p = base + (2.5 if i % 2 == 0 else -1.5)                     # oscillate around rising base
        rows.append((p, p + 0.6, p - 0.6, 3000.0))
    rows += [_base(c, 3000.0) for c in (110.0, 110.6, 111.0)]
    rows += [_base(110.4, 800.0), _base(110.2, 800.0)]               # shallow pullback
    rows.append((112.0, 112.4, 110.2, 30000.0))                     # break + heavy volume
    return rows


def _weak_rvol_rows(break_vol: float) -> list[tuple[float, float, float, float]]:
    """Same canonical change>=10% impulse/pullback/break as the backside frame, but the
    break-bar volume is modest so the trigger-bar vol_ratio lands well below the 5x floor —
    the weak-batch 'best of a dull tape' case E3 is meant to floor out."""
    rows = [_base(100.0) for _ in range(14)]
    rows += [_base(c) for c in (102.0, 104.0, 106.0, 108.0, 110.0)]
    rows += [_base(109.0, 800.0), _base(108.5, 800.0)]
    rows.append((110.6, 111.2, 109.6, break_vol))
    return rows


def _low_change_rows() -> list[tuple[float, float, float, float]]:
    """A front-side frame with a heavy-volume break (vol_ratio clears 5x) but only a ~+4%
    day move (below the 10% change floor) — E3 should floor it on day-change, not RVOL."""
    rows = [_base(100.0) for _ in range(14)]
    rows += [_base(c) for c in (101.0, 102.0, 102.5, 103.0, 103.5)]
    rows += [_base(102.8, 800.0), _base(102.6, 800.0)]
    rows.append((104.0, 104.5, 102.8, 20000.0))
    return rows


# ══════════════════════════════════════════════════════════════════════════════
# E1 — session-anchored BACKSIDE veto
# ══════════════════════════════════════════════════════════════════════════════

def test_e1_vetoes_backside_entry(monkeypatch) -> None:
    _isolate_verticality(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", True, raising=False)
    df = _df(_backside_chasing_top_rows())
    # sanity: this frame really is a backside read
    assert front_side_state(df).is_backside is True
    ok, reason, dbg = pullback_break_confirmation(df, entry_interval="5m")
    assert ok is False, (reason, dbg)
    assert reason == "backside_lifecycle_veto"
    assert dbg.get("front_side_state") == "chasing_top"


def test_e1_noop_on_front_side(monkeypatch) -> None:
    _isolate_verticality(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", True, raising=False)
    df = _df(_front_side_explosive_rows())
    # this frame is genuinely front-side -> E1 must NOT veto it
    assert front_side_state(df).is_backside is False
    ok, reason, _ = pullback_break_confirmation(df, entry_interval="5m")
    assert ok is True
    assert reason == "first_pullback_ok"


def test_e1_noop_on_thin_unknown_frame() -> None:
    """front_side_state fails OPEN (is_backside=False, reason=insufficient_bars) on a thin /
    degenerate frame, so the veto can never fire on warmup data."""
    thin = front_side_state(_df([_base(100.0) for _ in range(3)]))
    assert thin.is_backside is False
    assert thin.reason == "insufficient_bars"
    empty = front_side_state(pd.DataFrame([]))
    assert empty.is_backside is False
    assert empty.reason == "insufficient_bars"


def test_e1_flag_off_is_byte_identical(monkeypatch) -> None:
    _isolate_verticality(monkeypatch)
    # The backside frame that the veto rejects must fire UNCHANGED when the flag is OFF
    # (and the explosive floor held off, since this frame would also trip it).
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", False, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", False, raising=False)
    df = _df(_backside_chasing_top_rows())
    ok, reason, _ = pullback_break_confirmation(df, entry_interval="5m")
    assert ok is True
    assert reason == "first_pullback_ok"


# ══════════════════════════════════════════════════════════════════════════════
# E3 — EXPLOSIVE-FLOOR hard gate
# ══════════════════════════════════════════════════════════════════════════════

def test_e3_floors_out_weak_low_rvol(monkeypatch) -> None:
    _isolate_verticality(monkeypatch)
    # isolate E3: hold off the (independent) E1 veto so RVOL is the only variable.
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", False, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", True, raising=False)
    df = _df(_weak_rvol_rows(3000.0))
    ok, reason, dbg = pullback_break_confirmation(df, entry_interval="5m")
    assert ok is False, (reason, dbg)
    assert reason == "below_explosive_floor_rvol"
    assert dbg.get("explosive_floor_rvol_required") == 5.0
    assert dbg.get("explosive_floor_rvol") < 5.0


def test_e3_floors_out_low_day_change(monkeypatch) -> None:
    _isolate_verticality(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", False, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", True, raising=False)
    df = _df(_low_change_rows())
    ok, reason, dbg = pullback_break_confirmation(df, entry_interval="5m")
    assert ok is False, (reason, dbg)
    # RVOL clears (heavy break vol); the ~+4% day move trips the change floor.
    assert reason == "below_explosive_floor_change"
    assert dbg.get("explosive_floor_change_pct_required") == 10.0
    assert dbg.get("explosive_floor_change_pct") < 10.0


def test_e3_allows_genuine_explosive(monkeypatch) -> None:
    _isolate_verticality(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", True, raising=False)
    # RVOL ~7.3 (>=5) AND +12% day move (>=10) -> clears the floor and fires.
    df = _df(_front_side_explosive_rows())
    ok, reason, dbg = pullback_break_confirmation(df, entry_interval="5m")
    assert ok is True, (reason, dbg)
    assert reason == "first_pullback_ok"
    assert dbg.get("vol_ratio") >= 5.0


def test_e3_flag_off_lets_weak_through(monkeypatch) -> None:
    _isolate_verticality(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_backside_veto_enabled", False, raising=False)
    # Flag OFF -> the SAME low-RVOL frame that E3 floored now fires (byte-identical path).
    monkeypatch.setattr(settings, "chili_momentum_explosive_floor_enabled", False, raising=False)
    df = _df(_weak_rvol_rows(3000.0))
    ok, reason, _ = pullback_break_confirmation(df, entry_interval="5m")
    assert ok is True
    assert reason == "first_pullback_ok"


# ══════════════════════════════════════════════════════════════════════════════
# E2 — CATALYST GRADING (selection delta the flag-gated viability block consumes)
# ══════════════════════════════════════════════════════════════════════════════

def test_e2_suppresses_weak_catalyst() -> None:
    # WEAK (dilution/compliance/legal) -> NEGATIVE delta of the full catalyst tilt; the
    # viability block turns a negative delta into a live-eligibility drop (the hard gate).
    delta = catalyst_grade_selection_delta("DILU", weak_symbols={"DILU"}, strong_symbols=None)
    assert delta == -_catalyst_tilt()
    assert delta < 0


def test_e2_boosts_strong_catalyst() -> None:
    # STRONG (FDA/M&A/contract/beat) -> POSITIVE boost (half tilt — a confirming signal).
    delta = catalyst_grade_selection_delta("FDAX", weak_symbols=None, strong_symbols={"FDAX"})
    assert delta == _catalyst_tilt() * 0.5
    assert delta > 0
    # the strong classifier the boost set is built from
    assert _is_strong_catalyst("Company receives FDA approval for lead candidate") is True
    assert _is_strong_catalyst("Company schedules annual shareholder meeting") is False


def test_e2_neutral_on_medium_and_weak_dominates() -> None:
    # MEDIUM / no headline -> 0 (neutral).
    assert catalyst_grade_selection_delta(
        "MIDX", weak_symbols={"DILU"}, strong_symbols={"FDAX"}
    ) == 0.0
    # A name that is BOTH weak and strong is still a fade for Ross -> weak DOMINATES.
    assert catalyst_grade_selection_delta(
        "BOTH", weak_symbols={"BOTH"}, strong_symbols={"BOTH"}
    ) == -_catalyst_tilt()


def test_e2_noop_when_feed_absent_or_crypto() -> None:
    # Flag-equivalent OFF: an absent feed (no weak/strong sets) -> 0 (no change). This is the
    # exact condition under which the viability block skips entirely (`if _weak_g or _strong_g`).
    assert catalyst_grade_selection_delta("ABCD", weak_symbols=None, strong_symbols=None) == 0.0
    # Crypto is never graded (different 24h semantics, no equity catalysts).
    assert catalyst_grade_selection_delta(
        "BTC-USD", weak_symbols={"BTC"}, strong_symbols={"BTC"}
    ) == 0.0
