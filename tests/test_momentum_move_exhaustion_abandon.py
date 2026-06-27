"""Pre-arm MOVE-EXHAUSTION ABANDON — a RISK-REDUCING, agreement-gated veto that refuses to
arm a NEW watcher when a fresh entry trigger fires INTO a genuinely-exhausted move (Ross:
sit flat on a done move rather than chase the last leg). The danger is OVER-restriction
(blocking good fresh movers), so the gate is CONSERVATIVE + agreement-gated:

  ABANDON  iff  FADED-FROM-HOD  AND  (COLD-TAPE  OR  VIABILITY-REGRESSED).

Adversarial coverage:
  * exhausted (faded + cold + regressed)            => ABANDON
  * strong front-side (near HOD, hot, at-peak)      => ARMS  (never faded)
  * single flicker (faded, but tape hot + at-peak)  => ARMS  (agreement required)
  * flag OFF (default)                              => BYTE-IDENTICAL (gate never runs)

The agreement rule + the per-axis helpers are pure (no DB), so they run anywhere without
TEST_DATABASE_URL. The two _move_is_exhausted / _entry_trigger_fires paths monkeypatch the
(DB-bound) tape probe so they too stay DB-free.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from app.config import settings
from app.services.trading.momentum_neural import auto_arm
from app.services.trading.momentum_neural.auto_arm import (
    _VIABILITY_PEAK,
    _exhaustion_abandon_eligible,
    _faded_from_hod,
    _move_exhaustion_abandon_enabled,
    _move_is_exhausted,
    _row_ross_score,
    _update_viability_peak,
    _utcnow,
    _viability_regressed,
)
from app.services.trading.momentum_neural.ross_momentum import front_side_state


# ── helpers ─────────────────────────────────────────────────────────────────────────
def _df(closes, vol=1000):
    return pd.DataFrame({
        "Open": closes,
        "High": [c * 1.001 for c in closes],
        "Low": [c * 0.999 for c in closes],
        "Close": closes,
        "Volume": [vol] * len(closes),
    })


# A deep FADE off the HOD: open ~10, thrust to ~20 (HOD), retrace back to ~12.
# retrace_from_hod = (hod - last) / (hod - open) ≈ (20-12)/(20-10) = 0.8 > 0.66 floor.
_FADED_CLOSES = [10.0] * 8 + [13.0, 16.0, 20.0, 18.0, 15.0, 12.0]
# A FRESH front-side thrust that breaks to a NEW HIGH on the last bar: retrace ≈ 0.
_FRESH_CLOSES = [10.0] * 8 + [11.0, 12.0, 13.0, 14.0, 16.0, 20.0]


def _row(symbol: str, *, ross: float | None = None):
    """Fake MomentumSymbolViability carrying the persisted ross_scores[SYM] shape the gate
    reads (same as the event-based-abandonment + continuation helpers)."""
    extra: dict = {}
    if ross is not None:
        extra["ross_scores"] = {symbol.upper(): ross}
    return SimpleNamespace(symbol=symbol, execution_readiness_json={"extra": extra})


def _reset_peak():
    _VIABILITY_PEAK.clear()


# ── kill-switch parity ────────────────────────────────────────────────────────────────
def test_flag_off_by_default():
    # default OFF => the exhaustion gate never runs => arm-time is byte-identical.
    assert _move_exhaustion_abandon_enabled() is False


def test_flag_on_when_set(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_abandon_enabled", True)
    assert _move_exhaustion_abandon_enabled() is True


# ── faded-from-HOD axis (front_side_state.retrace_from_hod vs the adaptive floor) ──────
def test_faded_true_on_deep_retrace(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_retrace_floor", 0.66)
    fss = front_side_state(_df(_FADED_CLOSES), retrace_veto=0.66)
    assert fss.retrace_from_hod is not None and fss.retrace_from_hod > 0.66
    assert _faded_from_hod(fss) is True


def test_faded_false_near_hod(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_retrace_floor", 0.66)
    fss = front_side_state(_df(_FRESH_CLOSES), retrace_veto=0.66)
    assert _faded_from_hod(fss) is False


def test_faded_fail_open_on_missing(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_retrace_floor", 0.66)
    # None front-side state / None retrace => cannot prove faded => False (fail-open).
    assert _faded_from_hod(None) is False
    assert _faded_from_hod(SimpleNamespace(retrace_from_hod=None)) is False


def test_faded_disabled_when_floor_zero(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_retrace_floor", 0.0)
    fss = front_side_state(_df(_FADED_CLOSES), retrace_veto=0.66)
    assert _faded_from_hod(fss) is False  # 0 floor disables the axis (never faded)


# ── viability-regressed axis (current ross vs the in-process per-symbol peak) ──────────
def test_viability_regressed_true_below_peak(monkeypatch):
    _reset_peak()
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_regress_frac", 0.20)
    now = _utcnow()
    _update_viability_peak("ABCD", 0.90, now)             # peak 0.90
    # now 0.70 <= 0.90 * (1 - 0.20) = 0.72 => regressed.
    assert _viability_regressed("ABCD", 0.70, now) is True


def test_viability_not_regressed_at_peak(monkeypatch):
    _reset_peak()
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_regress_frac", 0.20)
    now = _utcnow()
    _update_viability_peak("ABCD", 0.90, now)
    # still at/near the peak (0.85 > 0.72 boundary) => NOT regressed.
    assert _viability_regressed("ABCD", 0.85, now) is False


def test_viability_regressed_fail_open(monkeypatch):
    _reset_peak()
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_regress_frac", 0.20)
    now = _utcnow()
    # no peak tracked / no current score => cannot prove regression => False (fail-open).
    assert _viability_regressed("NOPK", 0.50, now) is False
    _update_viability_peak("ABCD", 0.90, now)
    assert _viability_regressed("ABCD", None, now) is False


def test_viability_regressed_disabled_when_frac_zero(monkeypatch):
    _reset_peak()
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_regress_frac", 0.0)
    now = _utcnow()
    _update_viability_peak("ABCD", 0.90, now)
    assert _viability_regressed("ABCD", 0.10, now) is False  # axis off => never regressed


def test_peak_tracks_running_max(monkeypatch):
    _reset_peak()
    now = _utcnow()
    _update_viability_peak("ABCD", 0.50, now)
    _update_viability_peak("ABCD", 0.90, now)
    _update_viability_peak("ABCD", 0.60, now)             # a dip does NOT lower the peak
    assert _VIABILITY_PEAK["ABCD"][0] == 0.90


# ── THE agreement rule (conservative: faded AND (cold OR regressed)) ──────────────────
def test_agreement_rule_truth_table():
    # not faded => never abandon, regardless of the other axes.
    assert _exhaustion_abandon_eligible(False, True, True) is False
    assert _exhaustion_abandon_eligible(False, False, False) is False
    # faded alone (single flicker) => NOT enough agreement => arms.
    assert _exhaustion_abandon_eligible(True, False, False) is False
    # faded AND cold-tape => abandon.
    assert _exhaustion_abandon_eligible(True, True, False) is True
    # faded AND viability-regressed => abandon.
    assert _exhaustion_abandon_eligible(True, False, True) is True
    # faded AND both => abandon.
    assert _exhaustion_abandon_eligible(True, True, True) is True


# ── row ross-score reader ─────────────────────────────────────────────────────────────
def test_row_ross_score_reads_persisted_signal():
    assert _row_ross_score(_row("XYZ", ross=0.83)) == 0.83
    assert _row_ross_score(_row("XYZ")) is None          # absent => None (axis fail-open)


# ── end-to-end _move_is_exhausted (tape monkeypatched -> DB-free) ─────────────────────
def test_exhausted_abandons_faded_cold_regressed(monkeypatch):
    _reset_peak()
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_retrace_floor", 0.66)
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_regress_frac", 0.20)
    monkeypatch.setattr(auto_arm, "_tape_cold", lambda _s: True)  # tape gone cold
    now = _utcnow()
    _update_viability_peak("DONE", 0.90, now)            # prior peak high
    # current ross 0.60 (regressed off 0.90) on a deep-faded frame + cold tape => abandon.
    abandon, dbg = _move_is_exhausted("DONE", _df(_FADED_CLOSES), _row("DONE", ross=0.60))
    assert abandon is True
    assert dbg["faded_from_hod"] is True
    assert dbg["tape_cold"] is True
    assert dbg["viability_regressed"] is True


def test_strong_front_side_arms(monkeypatch):
    _reset_peak()
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_retrace_floor", 0.66)
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_regress_frac", 0.20)
    # even if the tape were cold, a near-HOD fresh mover is NEVER faded => never abandoned.
    monkeypatch.setattr(auto_arm, "_tape_cold", lambda _s: True)
    now = _utcnow()
    _update_viability_peak("HOT", 0.90, now)
    abandon, dbg = _move_is_exhausted("HOT", _df(_FRESH_CLOSES), _row("HOT", ross=0.92))
    assert abandon is False
    assert dbg["faded_from_hod"] is False


def test_single_flicker_arms_agreement_required(monkeypatch):
    _reset_peak()
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_retrace_floor", 0.66)
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_regress_frac", 0.20)
    monkeypatch.setattr(auto_arm, "_tape_cold", lambda _s: False)  # tape still HOT
    now = _utcnow()
    _update_viability_peak("FLKR", 0.90, now)
    # faded frame, but tape hot AND viability at peak (0.90) => only ONE axis => arms.
    abandon, dbg = _move_is_exhausted("FLKR", _df(_FADED_CLOSES), _row("FLKR", ross=0.90))
    assert abandon is False
    assert dbg["faded_from_hod"] is True
    assert dbg["tape_cold"] is False
    assert dbg["viability_regressed"] is False


def test_move_is_exhausted_never_raises(monkeypatch):
    _reset_peak()
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_abandon_enabled", True)
    # garbage frame / row => the gate swallows the error and PERMITS the arm (fail-open).
    abandon, _ = _move_is_exhausted("ERR", object(), object())
    assert abandon is False


# ── flag OFF => _entry_trigger_fires is byte-identical (gate never runs) ───────────────
def test_entry_trigger_byte_identical_when_flag_off(monkeypatch):
    # Flag OFF (default): force a firing trigger and a frame that WOULD be abandoned if the
    # gate ran; assert the result is the unchanged (True, reason) — the gate must be inert.
    import app.services.trading.momentum_neural.entry_gates as eg
    import app.services.trading.market_data as md

    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_abandon_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_entry_trigger_mode", "pullback_break")
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_trigger_parity_enabled", True)
    monkeypatch.setattr(md, "fetch_ohlcv_df", lambda *a, **k: _df(_FADED_CLOSES))
    monkeypatch.setattr(
        eg, "momentum_pullback_trigger", lambda *a, **k: (True, "pullback_break", {})
    )
    # A canary: if the gate ran it would call _move_is_exhausted; make that explode so a
    # regression (gate running while OFF) would be loud rather than silently passing.
    def _boom(*a, **k):  # pragma: no cover - only hit on regression
        raise AssertionError("exhaustion gate ran while flag OFF")
    monkeypatch.setattr(auto_arm, "_move_is_exhausted", _boom)

    fires, reason = auto_arm._entry_trigger_fires("BYTE", _row("BYTE", ross=0.50))
    assert fires is True
    assert reason == "pullback_break"


def test_entry_trigger_abandons_when_flag_on(monkeypatch):
    # Flag ON + a firing trigger into a faded+cold+regressed move => abandons (no arm).
    import app.services.trading.momentum_neural.entry_gates as eg
    import app.services.trading.market_data as md

    _reset_peak()
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_abandon_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_retrace_floor", 0.66)
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_regress_frac", 0.20)
    monkeypatch.setattr(settings, "chili_momentum_entry_trigger_mode", "pullback_break")
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_trigger_parity_enabled", True)
    monkeypatch.setattr(md, "fetch_ohlcv_df", lambda *a, **k: _df(_FADED_CLOSES))
    monkeypatch.setattr(
        eg, "momentum_pullback_trigger", lambda *a, **k: (True, "pullback_break", {})
    )
    monkeypatch.setattr(auto_arm, "_tape_cold", lambda _s: True)
    _update_viability_peak("DONE", 0.90, _utcnow())

    fires, reason = auto_arm._entry_trigger_fires("DONE", _row("DONE", ross=0.60))
    assert fires is False
    assert reason == "exhaustion_abandoned"


def test_entry_trigger_strong_mover_still_arms_when_flag_on(monkeypatch):
    # Flag ON but a STRONG front-side mover (near HOD) still arms — the conservative guarantee.
    import app.services.trading.momentum_neural.entry_gates as eg
    import app.services.trading.market_data as md

    _reset_peak()
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_abandon_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_retrace_floor", 0.66)
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_regress_frac", 0.20)
    monkeypatch.setattr(settings, "chili_momentum_entry_trigger_mode", "pullback_break")
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_trigger_parity_enabled", True)
    monkeypatch.setattr(md, "fetch_ohlcv_df", lambda *a, **k: _df(_FRESH_CLOSES))
    monkeypatch.setattr(
        eg, "momentum_pullback_trigger", lambda *a, **k: (True, "pullback_break", {})
    )
    monkeypatch.setattr(auto_arm, "_tape_cold", lambda _s: True)  # even with cold tape
    _update_viability_peak("HOT", 0.90, _utcnow())

    fires, reason = auto_arm._entry_trigger_fires("HOT", _row("HOT", ross=0.92))
    assert fires is True
    assert reason == "pullback_break"
