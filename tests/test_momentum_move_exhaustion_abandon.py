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

from datetime import timedelta
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


# ══════════════════════════════════════════════════════════════════════════════════════
# ADVERSARIAL HARDENING — every conditional branch, boundary (exactly-at / eps-below /
# eps-above), edge input (None/NaN/0/neg/empty/malformed), fail-open path, and flag parity.
# Each assertion targets the SPECIFIC reason/value so it fails if its branch regresses.
# ══════════════════════════════════════════════════════════════════════════════════════


# ── _faded_from_hod: BOUNDARY around the strict `rf > floor` comparison ─────────────────
def test_faded_boundary_exactly_at_floor_is_not_faded(monkeypatch):
    # rf == floor must be NOT faded (the comparison is STRICT `>`). A regression to `>=`
    # would flip a name sitting exactly on the floor into faded => over-restriction.
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_retrace_floor", 0.66)
    assert _faded_from_hod(SimpleNamespace(retrace_from_hod=0.66)) is False


def test_faded_boundary_eps_above_floor_is_faded(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_retrace_floor", 0.66)
    assert _faded_from_hod(SimpleNamespace(retrace_from_hod=0.6600001)) is True


def test_faded_boundary_eps_below_floor_is_not_faded(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_retrace_floor", 0.66)
    assert _faded_from_hod(SimpleNamespace(retrace_from_hod=0.6599999)) is False


def test_faded_fail_open_on_unparseable_retrace(monkeypatch):
    # A non-numeric retrace datum => float() raises => fail-OPEN (False), never blocks an arm.
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_retrace_floor", 0.66)
    assert _faded_from_hod(SimpleNamespace(retrace_from_hod="not-a-number")) is False


def test_faded_fail_open_on_none_floor_setting(monkeypatch):
    # A None floor setting collapses to 0.0 (`or 0.0`) => axis disabled => never faded even
    # on a deeply-retraced frame. Proves a missing knob can never over-veto.
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_retrace_floor", None)
    assert _faded_from_hod(SimpleNamespace(retrace_from_hod=0.99)) is False


# ── _viability_regressed: BOUNDARY around `now <= peak * (1 - frac)` ────────────────────
def test_regressed_boundary_exactly_at_threshold(monkeypatch):
    # now == peak*(1-frac) is `<=` => regressed (the threshold is INCLUSIVE on the low side).
    _reset_peak()
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_regress_frac", 0.20)
    now = _utcnow()
    _update_viability_peak("EDGE", 1.0, now)  # peak 1.0 -> threshold 0.80
    assert _viability_regressed("EDGE", 0.80, now) is True


def test_regressed_boundary_eps_above_threshold_not_regressed(monkeypatch):
    _reset_peak()
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_regress_frac", 0.20)
    now = _utcnow()
    _update_viability_peak("EDGE", 1.0, now)  # threshold 0.80
    assert _viability_regressed("EDGE", 0.8000001, now) is False


def test_regressed_stale_peak_fails_open(monkeypatch):
    # A peak older than the TTL cannot prove regression => False (fail-open). Defeats stale
    # conviction history blocking a fresh arm.
    _reset_peak()
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_regress_frac", 0.20)
    monkeypatch.setattr(settings, "chili_momentum_risk_viability_max_age_seconds", 600.0)
    now = _utcnow()
    _update_viability_peak("STALE", 1.0, now - timedelta(seconds=601))  # peak set in the past
    assert _viability_regressed("STALE", 0.10, now) is False


def test_regressed_zero_or_negative_peak_fails_open(monkeypatch):
    # A peak of 0 (or below) cannot define a meaningful drop fraction => fail-open.
    _reset_peak()
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_regress_frac", 0.20)
    now = _utcnow()
    _update_viability_peak("ZERO", 0.0, now)
    assert _viability_regressed("ZERO", -1.0, now) is False


def test_regressed_fail_open_on_unparseable_frac(monkeypatch):
    # A non-numeric regress_frac => float() raises => frac=0.0 => axis off => never regressed.
    _reset_peak()
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_regress_frac", "bad")
    now = _utcnow()
    _update_viability_peak("FRAC", 1.0, now)
    assert _viability_regressed("FRAC", 0.0, now) is False


# ── _update_viability_peak: stale-rebuild, no-op guards, max-keeping ────────────────────
def test_peak_stale_rebuilds_from_lower_fresh_score(monkeypatch):
    # A peak older than the TTL must be DROPPED and rebuilt from the current (even lower)
    # score, so a name that fell out and resurged tracks its NEW peak, not a phantom old high.
    _reset_peak()
    monkeypatch.setattr(settings, "chili_momentum_risk_viability_max_age_seconds", 600.0)
    now = _utcnow()
    _update_viability_peak("RB", 0.90, now - timedelta(seconds=601))  # stale high
    _update_viability_peak("RB", 0.30, now)                            # fresh, lower
    assert _VIABILITY_PEAK["RB"][0] == 0.30  # rebuilt, NOT max(0.90, 0.30)


def test_peak_within_window_keeps_running_max(monkeypatch):
    _reset_peak()
    monkeypatch.setattr(settings, "chili_momentum_risk_viability_max_age_seconds", 600.0)
    now = _utcnow()
    _update_viability_peak("MX", 0.90, now)
    _update_viability_peak("MX", 0.30, now + timedelta(seconds=10))  # within window, lower
    assert _VIABILITY_PEAK["MX"][0] == 0.90  # max kept


def test_peak_noop_on_empty_symbol_or_none_score():
    _reset_peak()
    now = _utcnow()
    _update_viability_peak("", 0.90, now)          # empty symbol => no-op
    _update_viability_peak("X", None, now)         # None score   => no-op
    assert "" not in _VIABILITY_PEAK
    assert "X" not in _VIABILITY_PEAK


def test_peak_noop_on_unparseable_score():
    _reset_peak()
    now = _utcnow()
    _update_viability_peak("BAD", "not-a-number", now)
    assert "BAD" not in _VIABILITY_PEAK


# ── _move_exhaustion_peak_ttl_seconds: fail-safe default ────────────────────────────────
def test_ttl_defaults_on_unparseable_setting(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_risk_viability_max_age_seconds", "oops")
    assert auto_arm._move_exhaustion_peak_ttl_seconds() == 600.0


# ── _row_ross_score: malformed/edge json shapes ─────────────────────────────────────────
def test_row_ross_score_case_insensitive_symbol():
    # The map is keyed UPPER; a lowercase row.symbol must still resolve its score.
    row = SimpleNamespace(symbol="abc", execution_readiness_json={"extra": {"ross_scores": {"ABC": 0.77}}})
    assert _row_ross_score(row) == 0.77


def test_row_ross_score_non_dict_ross_scores_is_none():
    # ross_scores present but NOT a dict => guarded to {} => None (axis fail-open).
    row = SimpleNamespace(symbol="ABC", execution_readiness_json={"extra": {"ross_scores": [1, 2, 3]}})
    assert _row_ross_score(row) is None


def test_row_ross_score_none_json_is_none():
    row = SimpleNamespace(symbol="ABC", execution_readiness_json=None)
    assert _row_ross_score(row) is None


def test_row_ross_score_none_value_coerces_zero():
    # A present-but-None score coerces via `float(... or 0.0)` to 0.0 (not None).
    row = SimpleNamespace(symbol="ABC", execution_readiness_json={"extra": {"ross_scores": {"ABC": None}}})
    assert _row_ross_score(row) == 0.0


# ── _tape_cold: signed-tape definition, crypto exemption, fail-open (DB-free) ────────────
def _patch_tape(monkeypatch, tape):
    """Patch the (DB-bound) tape probe + a no-op SessionLocal so _tape_cold runs DB-free."""
    import app.services.trading.momentum_neural.entry_gates as eg
    import app.db as _db

    monkeypatch.setattr(eg, "signed_tape_accel_features", lambda *a, **k: tape)
    monkeypatch.setattr(_db, "SessionLocal", lambda: SimpleNamespace(close=lambda: None))


def test_tape_cold_crypto_is_never_cold():
    # Crypto (-USD) has no equity tick tape => fail-open (NOT cold) WITHOUT touching the DB.
    assert auto_arm._tape_cold("BTC-USD") is False


def test_tape_cold_empty_symbol_is_never_cold():
    assert auto_arm._tape_cold("") is False
    assert auto_arm._tape_cold(None) is False  # type: ignore[arg-type]


def test_tape_cold_accel_le_zero_is_cold(monkeypatch):
    # signed_tape_accel <= 0 (not accelerating into the buy) => COLD.
    _patch_tape(monkeypatch, {"signed_tape_accel": -0.5, "tick_rate": 99.0, "tick_rate_floor": 1.0})
    assert auto_arm._tape_cold("ABC") is True


def test_tape_cold_accel_exactly_zero_is_cold(monkeypatch):
    # BOUNDARY: accel == 0 is `<= 0` => cold (a flat tape is not accelerating).
    _patch_tape(monkeypatch, {"signed_tape_accel": 0.0, "tick_rate": 99.0, "tick_rate_floor": 1.0})
    assert auto_arm._tape_cold("ABC") is True


def test_tape_cold_rate_below_floor_is_cold(monkeypatch):
    # accel positive BUT tick_rate below its self-relative floor => activity collapsed => cold.
    _patch_tape(monkeypatch, {"signed_tape_accel": 1.0, "tick_rate": 0.5, "tick_rate_floor": 1.0})
    assert auto_arm._tape_cold("ABC") is True


def test_tape_hot_accel_pos_and_rate_above_floor(monkeypatch):
    # accel > 0 AND rate >= floor => HOT (not cold). The agreement axis stays quiet.
    _patch_tape(monkeypatch, {"signed_tape_accel": 1.0, "tick_rate": 5.0, "tick_rate_floor": 1.0})
    assert auto_arm._tape_cold("ABC") is False


def test_tape_cold_rate_at_floor_is_hot(monkeypatch):
    # BOUNDARY: rate == floor is `rate < floor` False => NOT cold on the rate axis.
    _patch_tape(monkeypatch, {"signed_tape_accel": 1.0, "tick_rate": 1.0, "tick_rate_floor": 1.0})
    assert auto_arm._tape_cold("ABC") is False


def test_tape_cold_zero_floor_disables_rate_axis(monkeypatch):
    # floor <= 0 => the rate sub-condition is OFF (only accel governs); a positive accel => hot.
    _patch_tape(monkeypatch, {"signed_tape_accel": 1.0, "tick_rate": 0.0, "tick_rate_floor": 0.0})
    assert auto_arm._tape_cold("ABC") is False


def test_tape_cold_non_dict_fails_open(monkeypatch):
    # No/thin tape (None / non-dict) => fail-open (not cold) so missing tape never blocks an arm.
    _patch_tape(monkeypatch, None)
    assert auto_arm._tape_cold("ABC") is False
    _patch_tape(monkeypatch, "garbage")
    assert auto_arm._tape_cold("ABC") is False


def test_tape_cold_unparseable_fields_fail_open(monkeypatch):
    # Non-numeric tape fields => float() raises => fail-open (not cold).
    _patch_tape(monkeypatch, {"signed_tape_accel": "x", "tick_rate": "y", "tick_rate_floor": "z"})
    assert auto_arm._tape_cold("ABC") is False


# ── _move_is_exhausted: short-circuit, dbg payload, agreement combinations (DB-free) ────
def test_move_is_exhausted_short_circuits_tape_when_not_faded(monkeypatch):
    # A near-HOD (NOT faded) frame must SKIP the DB-bound tape probe entirely (cheap path).
    # If _tape_cold is consulted while not faded, this canary raises => loud regression.
    _reset_peak()
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_retrace_floor", 0.66)
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_regress_frac", 0.20)

    def _canary(_s):  # pragma: no cover - only hit on regression
        raise AssertionError("tape probed on a non-faded (near-HOD) frame")

    monkeypatch.setattr(auto_arm, "_tape_cold", _canary)
    abandon, dbg = _move_is_exhausted("FRESH", _df(_FRESH_CLOSES), _row("FRESH", ross=0.92))
    assert abandon is False
    assert dbg["faded_from_hod"] is False
    assert dbg["tape_cold"] is False           # default, NOT computed
    assert dbg["viability_regressed"] is False  # default, NOT computed


def test_move_is_exhausted_faded_only_arms_with_dbg(monkeypatch):
    # faded but tape HOT and at-peak => single flicker => arms; dbg reflects the axes.
    _reset_peak()
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_retrace_floor", 0.66)
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_regress_frac", 0.20)
    monkeypatch.setattr(auto_arm, "_tape_cold", lambda _s: False)
    now = _utcnow()
    _update_viability_peak("FLK", 0.90, now)
    abandon, dbg = _move_is_exhausted("FLK", _df(_FADED_CLOSES), _row("FLK", ross=0.90))
    assert abandon is False
    assert dbg["faded_from_hod"] is True
    assert dbg["abandon"] is False
    assert dbg["ross_now"] == 0.90
    assert dbg["retrace_from_hod"] is not None and dbg["retrace_from_hod"] > 0.66


def test_move_is_exhausted_faded_plus_regressed_only_abandons(monkeypatch):
    # faded + viability-regressed (tape HOT) => the OR-arm via regression alone abandons.
    _reset_peak()
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_retrace_floor", 0.66)
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_regress_frac", 0.20)
    monkeypatch.setattr(auto_arm, "_tape_cold", lambda _s: False)  # tape stays HOT
    now = _utcnow()
    _update_viability_peak("REG", 1.0, now)  # peak 1.0 -> regressed threshold 0.80
    abandon, dbg = _move_is_exhausted("REG", _df(_FADED_CLOSES), _row("REG", ross=0.50))
    assert abandon is True
    assert dbg["tape_cold"] is False
    assert dbg["viability_regressed"] is True


def test_move_is_exhausted_peak_refreshed_before_regression_test(monkeypatch):
    # The peak is refreshed with THIS pass's score FIRST, so a name still PRINTING a new high
    # is never "regressed" against itself even if a stale lower peak existed.
    _reset_peak()
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_retrace_floor", 0.66)
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_regress_frac", 0.20)
    monkeypatch.setattr(auto_arm, "_tape_cold", lambda _s: True)  # cold, so only regression gates
    now = _utcnow()
    _update_viability_peak("UP", 0.50, now)  # a lower prior peak
    # current 0.95 RAISES the peak to 0.95 first => 0.95 <= 0.95*0.8 is False => not regressed.
    abandon, dbg = _move_is_exhausted("UP", _df(_FADED_CLOSES), _row("UP", ross=0.95))
    assert dbg["viability_regressed"] is False
    # faded + cold (tape) still abandons via the cold-tape arm — but regression must be clean.
    assert dbg["faded_from_hod"] is True
    assert abandon is True  # cold-tape arm
    assert _VIABILITY_PEAK["UP"][0] == 0.95  # peak raised by this pass


def test_move_is_exhausted_fail_open_on_garbage():
    # Garbage df/row inputs are handled GRACEFULLY (each inner read is guarded): _row_ross_score
    # returns None, front_side_state's own try/except yields fss=None => not faded => the tape
    # and regression axes short-circuit and the function returns via the NORMAL fail-open path
    # (abandon=False), NOT the except path. The load-bearing property is fail-open: abandon is
    # False and dbg["abandon"] is False. (The except path — reason="exhaustion_error" — fires
    # only on an actual raise inside the try body, which guarded object() inputs never trigger.)
    _reset_peak()
    abandon, dbg = _move_is_exhausted("ERR", object(), object())
    assert abandon is False
    assert dbg.get("abandon") is False


# ── _entry_trigger_fires: CONTINUATION branch also carries the same veto (flag ON) ──────
def test_entry_trigger_continuation_branch_abandons_when_flag_on(monkeypatch):
    # The pullback probe does NOT fire (straight-up runner gives no base), but the
    # continuation trigger DOES; a faded+cold+regressed runner must still be abandoned on
    # the continuation arm — the SECOND veto site (auto_arm.py ~L2651), not just the first.
    import app.services.trading.momentum_neural.entry_gates as eg
    import app.services.trading.market_data as md

    _reset_peak()
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_abandon_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_retrace_floor", 0.66)
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_regress_frac", 0.20)
    monkeypatch.setattr(settings, "chili_momentum_entry_trigger_mode", "pullback_break")
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_trigger_parity_enabled", True)
    monkeypatch.setattr(md, "fetch_ohlcv_df", lambda *a, **k: _df(_FADED_CLOSES))
    monkeypatch.setattr(eg, "momentum_pullback_trigger", lambda *a, **k: (False, "no_base", {}))
    monkeypatch.setattr(auto_arm, "_continuation_active_trigger", lambda *a, **k: (True, "continuation"))
    monkeypatch.setattr(auto_arm, "_tape_cold", lambda _s: True)
    _update_viability_peak("CONT", 0.90, _utcnow())

    fires, reason = auto_arm._entry_trigger_fires("CONT", _row("CONT", ross=0.60))
    assert fires is False
    assert reason == "exhaustion_abandoned"


def test_entry_trigger_continuation_byte_identical_when_flag_off(monkeypatch):
    # Flag OFF: the continuation arm must be byte-identical — the gate is inert even on a
    # frame that WOULD be abandoned. Canary explodes if the gate runs.
    import app.services.trading.momentum_neural.entry_gates as eg
    import app.services.trading.market_data as md

    _reset_peak()
    monkeypatch.setattr(settings, "chili_momentum_move_exhaustion_abandon_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_entry_trigger_mode", "pullback_break")
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_trigger_parity_enabled", True)
    monkeypatch.setattr(md, "fetch_ohlcv_df", lambda *a, **k: _df(_FADED_CLOSES))
    monkeypatch.setattr(eg, "momentum_pullback_trigger", lambda *a, **k: (False, "no_base", {}))
    monkeypatch.setattr(auto_arm, "_continuation_active_trigger", lambda *a, **k: (True, "continuation"))

    def _boom(*a, **k):  # pragma: no cover - only hit on regression
        raise AssertionError("exhaustion gate ran while flag OFF (continuation branch)")

    monkeypatch.setattr(auto_arm, "_move_is_exhausted", _boom)
    fires, reason = auto_arm._entry_trigger_fires("CONT", _row("CONT", ross=0.50))
    assert fires is True
    assert reason == "continuation"
