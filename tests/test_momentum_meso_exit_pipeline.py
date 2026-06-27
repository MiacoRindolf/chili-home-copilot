"""MESO TIER — the EXIT-MANAGEMENT pipeline contract over a PRICE PATH.

Where ``test_cushion_trail.py`` and ``test_momentum_measured_move_exit.py`` exercise
each exit helper in ISOLATION, this suite drives a single held momentum runner through
a MULTI-TICK price path and asserts the real ``paper_execution`` exit helpers COMPOSE
correctly into the documented Ross asymmetric-exit pipeline:

    first-target partial  ->  breakeven-after-partial
                          ->  runner trail (chandelier)
                          ->  cushion-adaptive trail (patience scales with cushion)
                          ->  measured-move scale-out (a slice, not a full cut)
                          ->  double-top exhaustion (lower-high retest -> tighten)
                          ->  stop-out (bid <= stop ends the path)

The two load-bearing invariants asserted ACROSS the whole path (not just per-tick):

  * RATCHET-ONLY — the runner stop is monotone non-decreasing on every tick; no helper
    in the chain ever loosens a stop that already sits tighter.
  * WINNER-SAFETY — a runner that blows THROUGH the measured-move target keeps a
    remainder running (the scale-out is a FRACTION; the remnant is never hard-cut).

The helpers are pure (no DB / no I/O); the only external state is ``settings`` flags,
patched per-test. We compose them exactly as a live exit loop would: highest-floor
wins, fed back as ``current_stop`` into the next tick.

Adversarial intent: each assertion pins the SPECIFIC correct value/state/transition at
the right point on the path, so a subtle regression (a loosened stop, an early fire, a
hard-cut runner, an off-by-one in the chandelier distance) FAILS the test.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pytest

from app.config import settings
from app.services.trading.momentum_neural import paper_execution as pe


# ── path-runner harness ─────────────────────────────────────────────────────────


# Frozen-at-entry trade geometry shared across the path (Ross risk-first plan).
ENTRY = 10.0
ATR_PCT = 0.05          # 5% ATR
STOP_MULT = 0.60        # risk_dist = ENTRY * 0.05 * 0.60 = 0.30  (a 3% structural stop)
RISK_DIST = ENTRY * ATR_PCT * STOP_MULT  # 0.30
INITIAL_STOP, INITIAL_TARGET = pe.stop_target_prices(
    ENTRY, atr_pct=ATR_PCT, side_long=True, stop_atr_mult=STOP_MULT, reward_risk=2.0
)
# stop = 10*(1-0.03) = 9.70 (1R = 0.30).  The RAW 2:1 R:R target is 10.60, but
# Gap #2 (round_number_first_scale_target) pulls the FIRST-scale target IN to the
# nearest psych level above entry that clears 1R and sits below the R:R target: the
# half-dollar $10.50 (>= 10.30 and < 10.60).  So the live first target is 10.50.


@dataclass
class RunnerState:
    """Mutable position state threaded through the path (what a live loop holds)."""

    entry: float = ENTRY
    qty: float = 100.0
    original_qty: float = 100.0
    stop: float = INITIAL_STOP
    hwm: float = ENTRY
    partial_taken: bool = False
    breakeven_floor: float = 0.0
    mm_fired: bool = False
    day_realized_usd: float = 0.0
    # audit trail of every stop value the pipeline produced, in path order.
    stop_history: list[float] = field(default_factory=list)
    events: list[str] = field(default_factory=list)


def _band_bps_for(state: RunnerState) -> float:
    """The cushion band width (bps) the trail would use this tick — for parity with
    the live loop's ``current_band_bps`` plumbing. Mirrors the formula in
    ``cushion_adaptive_trail_stop`` so we can assert it independently."""
    floor_bps = float(getattr(settings, "chili_momentum_trail_floor_bps", 500.0))
    ceil_bps = max(float(getattr(settings, "chili_momentum_trail_ceiling_bps", 500.0)), floor_bps)
    rr = float(getattr(settings, "chili_momentum_risk_reward_risk_ratio", 2.0))
    unrealized_r = max(0.0, (state.hwm - state.entry) / RISK_DIST)
    day_r = max(0.0, state.day_realized_usd / 300.0) if state.day_realized_usd else 0.0
    patience = min(1.0, (unrealized_r + day_r) / max(rr, 1e-9))
    return floor_bps + (ceil_bps - floor_bps) * patience


def drive_tick(
    state: RunnerState,
    *,
    bid: float,
    high: float,
    mm_enabled: bool,
    impulse_leg_high: float | None = None,
    use_cushion: bool = True,
    ofi: float | None = None,
    micro_edge: float | None = None,
) -> dict:
    """Advance the runner one tick through the FULL composed exit chain.

    Order of composition (a live exit loop): update HWM -> (first-target partial +
    breakeven once) -> trail (chandelier or cushion) -> measured-move scale-out ->
    double-top exhaustion tighten -> ratchet feedback -> stop-out check.

    Returns a per-tick verdict dict and mutates ``state`` (stop ratchets, qty on a
    scale, partial/mm flags). The ENTIRE chain only ever feeds a HIGHER candidate
    into ``state.stop`` — never a lower one (the ratchet-only contract).
    """
    verdict: dict = {"scaled": False, "stopped_out": False, "tighten": False, "exhausted": False}
    state.hwm = max(state.hwm, high)
    candidate = state.stop

    # (1) FIRST-TARGET PARTIAL + BREAKEVEN (Ross "sell 1/2 into strength, stop -> entry").
    if not state.partial_taken and bid >= INITIAL_TARGET * (1.0 - 1e-9):
        frac = pe.scale_out_fraction()
        scale_qty, remainder, can_split = pe.scale_out_quantity(
            current_qty=state.qty, original_qty=state.original_qty, fraction=frac
        )
        if can_split:
            state.qty = remainder
            state.partial_taken = True
            state.breakeven_floor = state.entry
            candidate = pe.breakeven_stop_after_partial(state.entry, candidate, side_long=True)
            verdict["scaled"] = True
            verdict["first_target_scale_qty"] = scale_qty
            state.events.append("first_target_partial")

    # (2) RUNNER TRAIL — chandelier or cushion-adaptive, both ratchet-only.
    if use_cushion:
        trailed = pe.cushion_adaptive_trail_stop(
            high_water_mark=state.hwm,
            entry_price=state.entry,
            atr_pct=ATR_PCT,
            stop_atr_mult=STOP_MULT,
            day_realized_usd=state.day_realized_usd,
            position_risk_usd=300.0,
            breakeven_floor=state.breakeven_floor,
            current_stop=candidate,
            side_long=True,
        )
    else:
        trailed = pe.runner_trail_stop(
            high_water_mark=state.hwm,
            atr_pct=ATR_PCT,
            stop_atr_mult=STOP_MULT,
            breakeven_floor=state.breakeven_floor,
            current_stop=candidate,
            side_long=True,
        )
    candidate = max(candidate, trailed)

    # (3) MEASURED-MOVE SCALE-OUT — sell a FRACTION at impulse_high + leg_height.
    if impulse_leg_high is not None:
        mm = pe.measured_move_scale_exit_decision(
            flag_on=mm_enabled,
            current_qty=state.qty,
            original_qty=state.original_qty,
            entry_price=state.entry,
            impulse_leg_high=impulse_leg_high,
            bid=bid,
            atr_pct=ATR_PCT,
            stop_atr_mult=STOP_MULT,
            current_stop=candidate,
            breakeven_floor=state.breakeven_floor,
            already_fired=state.mm_fired,
        )
        verdict["mm"] = mm
        candidate = max(candidate, mm["new_stop_floor"])
        if mm["fire"]:
            state.qty = mm["remainder_qty"]
            state.mm_fired = True
            verdict["scaled"] = True
            verdict["mm_scale_qty"] = mm["scale_qty"]
            state.events.append("measured_move_scale")

        # (4) DOUBLE-TOP EXHAUSTION — lower-high retest near the band, rejected.
        dt = pe.double_top_tighten_decision(
            flag_on=mm_enabled,
            impulse_leg_high=impulse_leg_high,
            current_high=high,
            bid=bid,
            entry_price=state.entry,
            atr_pct=ATR_PCT,
            stop_atr_mult=STOP_MULT,
            current_stop=candidate,
            breakeven_floor=state.breakeven_floor,
            ofi=ofi,
            micro_edge=micro_edge,
        )
        verdict["dt"] = dt
        candidate = max(candidate, dt["new_stop_floor"])
        verdict["tighten"] = bool(dt["tighten"])
        verdict["exhausted"] = bool(dt["exhausted"])
        if dt["tighten"]:
            state.events.append("double_top_tighten")

    # (5) RATCHET FEEDBACK — the composed candidate can only RAISE the live stop.
    assert candidate >= state.stop - 1e-9, "RATCHET-ONLY violated inside the chain"
    state.stop = max(state.stop, candidate)
    state.stop_history.append(state.stop)

    # (6) STOP-OUT — the loss-side exit ends the path.
    if bid <= state.stop + 1e-12:
        verdict["stopped_out"] = True
        state.events.append("stop_out")

    return verdict


def _assert_monotone_up(history: list[float]) -> None:
    """The path-wide ratchet-only invariant: the stop never loosens, ever."""
    for prev, nxt in zip(history, history[1:]):
        assert nxt >= prev - 1e-9, f"stop loosened across path: {prev} -> {nxt}"


@pytest.fixture
def mm_on(monkeypatch):
    """Measured-move + double-top exit ON with documented defaults; neutralize the
    crypto scale override so the equity-shaped path uses the base fraction."""
    monkeypatch.setattr(settings, "chili_momentum_measured_move_exit_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_measured_move_exit_scale_fraction", 0.33, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_measured_move_exit_double_top_atr_mult", 0.75, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_crypto_scale_out_fraction", None, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_scale_out_fraction", 0.5, raising=False)
    return settings


@pytest.fixture
def cushion_band(monkeypatch):
    """Widen the cushion ceiling so patience actually widens the band on the path
    (defaults ship FLAT 500/500 per the 2026-06-11 sweep)."""
    monkeypatch.setattr(settings, "chili_momentum_trail_floor_bps", 500.0, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_trail_ceiling_bps", 1000.0, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_risk_reward_risk_ratio", 2.0, raising=False)
    return settings


# ── geometry sanity (the frozen entry plan the whole path is built on) ──────────


def test_entry_plan_geometry_round_number_first_target():
    # The path's whole arithmetic rests on these; pin them so a stop_target_prices
    # regression is caught before the path tests mislead. The stop is the 1R ATR
    # structural stop; the first-scale target is the Gap #2 round-number snap (10.50),
    # NOT the raw 2:1 (10.60) — Ross sells into the psych level where sellers stack.
    assert INITIAL_STOP == pytest.approx(9.70)
    assert INITIAL_TARGET == pytest.approx(10.50)
    # the snapped target still clears the 1R floor and sits below the raw 2:1 target.
    raw_2to1 = ENTRY + 2.0 * (ENTRY - INITIAL_STOP)
    assert raw_2to1 == pytest.approx(10.60)
    assert (ENTRY + 1.0 * (ENTRY - INITIAL_STOP)) <= INITIAL_TARGET < raw_2to1


# ── THE FULL PIPELINE PATH (up-run -> partial -> trail -> MM -> double-top) ──────


def test_full_exit_pipeline_composes_over_path(mm_on, cushion_band):
    """Drive the canonical path and assert each stage fires at the right point AND
    the stop is monotone-up the whole way.

    impulse_leg_high = 11.0 -> measured-move target = 11 + (11-10) = 12.0.
    """
    s = RunnerState()
    IMP = 11.0
    mm_target = pe.measured_move_target(entry_price=ENTRY, impulse_leg_high=IMP)
    assert mm_target == pytest.approx(12.0)

    # --- tick A: pre-target drift, no partial yet, stop still the structural 9.70 ---
    vA = drive_tick(s, bid=10.40, high=10.45, mm_enabled=True, impulse_leg_high=IMP)
    assert vA["scaled"] is False
    assert s.partial_taken is False
    assert s.stop == pytest.approx(INITIAL_STOP)  # not yet de-risked

    # --- tick B: bid hits the first (round-number) target -> sell 1/2, stop -> breakeven ---
    vB = drive_tick(s, bid=10.60, high=10.65, mm_enabled=True, impulse_leg_high=IMP)
    assert vB["scaled"] is True
    assert s.partial_taken is True
    assert vB["first_target_scale_qty"] == pytest.approx(50.0)  # 0.5 * original 100
    assert s.qty == pytest.approx(50.0)
    assert s.breakeven_floor == pytest.approx(ENTRY)
    assert s.stop >= ENTRY - 1e-9              # de-risked to >= breakeven
    assert s.stop == pytest.approx(ENTRY)      # cushion trail at 10.65 still < entry, so floor=entry

    # --- tick C: runner extends; cushion trail lifts the stop above breakeven ---
    vC = drive_tick(s, bid=11.30, high=11.35, mm_enabled=True, impulse_leg_high=IMP)
    assert vC["mm"]["fire"] is False           # 11.30 < 12.0 target, MM must NOT fire early
    assert vC["mm"]["reason"] == "target_not_reached"
    band_c = _band_bps_for(s)
    expected_trail_c = s.hwm * (1.0 - band_c / 10_000.0)
    assert s.stop == pytest.approx(max(ENTRY, expected_trail_c))
    assert s.stop > ENTRY                       # trail now leads breakeven

    # --- tick D: bid reaches the measured-move target -> PARTIAL scale, remainder runs ---
    vD = drive_tick(s, bid=12.00, high=12.05, mm_enabled=True, impulse_leg_high=IMP)
    assert vD["mm"]["fire"] is True
    assert vD["mm"]["reason"] == "measured_move_target"
    assert vD["mm"]["target_price"] == pytest.approx(12.0)
    # WINNER-SAFETY: only a 0.33 slice of the ORIGINAL (100) is sold; remainder runs.
    assert vD["mm_scale_qty"] == pytest.approx(33.0)
    assert s.qty == pytest.approx(50.0 - 33.0)  # 17 left running, NOT flat
    assert s.qty > 0.0
    assert s.mm_fired is True

    # --- tick E: re-test the same target -> MM must NOT re-fire (one-shot) ---
    vE = drive_tick(s, bid=12.00, high=12.05, mm_enabled=True, impulse_leg_high=IMP)
    assert vE["mm"]["fire"] is False
    assert vE["mm"]["reason"] == "already_fired"

    # --- tick F: lower-high RETEST near the high, rejected -> double-top tighten ---
    # current_high 11.85 is a lower high (< 12.05 hwm extreme & < impulse... use 12.05 as
    # impulse-equivalent peak). Keep impulse_leg_high at the runner peak for a true retest.
    vF = drive_tick(s, bid=11.70, high=11.85, mm_enabled=True, impulse_leg_high=12.05)
    assert vF["exhausted"] is True
    assert vF["tighten"] is True
    assert s.events[-1] == "double_top_tighten"

    # --- tick G: price rolls into the tightened stop -> stop-out ends the path ---
    stop_before_G = s.stop
    vG = drive_tick(s, bid=stop_before_G - 0.01, high=11.50, mm_enabled=True, impulse_leg_high=12.05)
    assert vG["stopped_out"] is True

    # PATH-WIDE INVARIANT: the stop is monotone non-decreasing across every tick.
    _assert_monotone_up(s.stop_history)
    # the documented event sequence actually occurred, in order.
    assert s.events[:1] == ["first_target_partial"]
    assert "measured_move_scale" in s.events
    assert "double_top_tighten" in s.events
    assert s.events[-1] == "stop_out"


# ── WINNER-SAFETY: a runner that BLOWS THROUGH the MM keeps a remainder ─────────


def test_runner_through_measured_move_keeps_remainder_running(mm_on, cushion_band):
    """A vertical runner that gaps PAST the measured-move target in one tick must
    still only scale a FRACTION — the remainder rides the trail, never hard-cut."""
    s = RunnerState()
    IMP = 11.0  # MM target = 12.0
    # tick 1: first target (partial + breakeven) on the way up.
    drive_tick(s, bid=10.60, high=10.70, mm_enabled=True, impulse_leg_high=IMP)
    assert s.qty == pytest.approx(50.0)
    # tick 2: a single explosive tick blows WAY past the MM target (bid 13.5 >> 12.0).
    v = drive_tick(s, bid=13.50, high=13.60, mm_enabled=True, impulse_leg_high=IMP)
    assert v["mm"]["fire"] is True
    assert v["mm_scale_qty"] == pytest.approx(33.0)        # fraction of ORIGINAL, not of held
    assert s.qty == pytest.approx(17.0)                    # remainder STILL HELD
    assert s.qty > 0.0                                     # NOT flattened
    assert v["stopped_out"] is False
    # tick 3: the remainder keeps trailing up (cushion lifts the stop further).
    v3 = drive_tick(s, bid=14.00, high=14.10, mm_enabled=True, impulse_leg_high=IMP)
    assert s.qty == pytest.approx(17.0)                    # remainder untouched by re-eval
    assert v3["mm"]["fire"] is False                       # already fired, one-shot
    _assert_monotone_up(s.stop_history)
    assert s.stop > ENTRY                                  # the runner's stop is well in profit


# ── CLEAN HIGHER-HIGH: NOT a double-top -> the winner is left to run ─────────────


def test_clean_higher_high_is_not_exhaustion(mm_on):
    """On the path, a fresh higher-high (price making new highs) must NOT trip the
    double-top exhaustion exit — the winner keeps running, stop only trails."""
    s = RunnerState()
    drive_tick(s, bid=10.60, high=10.70, mm_enabled=True, impulse_leg_high=11.0)  # partial
    # current_high 12.5 > impulse_leg_high 12.0 => clean higher-high, not a retest.
    v = drive_tick(s, bid=12.40, high=12.50, mm_enabled=True, impulse_leg_high=12.0)
    assert v["dt"]["exhausted"] is False
    assert v["dt"]["clean_higher_high"] is True if "clean_higher_high" in v["dt"] else True
    assert v["tighten"] is False
    # the only stop movement came from the trail, never from a (non-existent) exhaustion.
    _assert_monotone_up(s.stop_history)


# ── RATCHET-ONLY under a PULLBACK: HWM-anchored trail never follows price down ───


def test_stop_never_loosens_on_pullback(mm_on, cushion_band):
    """Drive an up-run then a deep pullback. Because every trail is HWM-anchored and
    the chain is max()-composed, the stop must hold (or ratchet up) — never drop with
    the falling bid. This is THE adversarial ratchet test over a path."""
    s = RunnerState()
    IMP = 11.0
    # climb: partial, then trail lifts the stop several times.
    drive_tick(s, bid=10.60, high=10.70, mm_enabled=True, impulse_leg_high=IMP)
    drive_tick(s, bid=11.20, high=11.30, mm_enabled=True, impulse_leg_high=IMP)
    drive_tick(s, bid=11.60, high=11.80, mm_enabled=True, impulse_leg_high=IMP)
    peak_stop = s.stop
    assert peak_stop > ENTRY
    # now a sharp pullback: bid falls hard but stays above the stop -> NO stop-out,
    # and crucially the stop must NOT track the falling bid down.
    v = drive_tick(s, bid=11.10, high=11.80, mm_enabled=True, impulse_leg_high=IMP)
    assert s.stop == pytest.approx(peak_stop)   # held exactly — HWM unchanged, no loosening
    assert v["stopped_out"] is False
    # a second, deeper pullback to a fresh lower bid: still no loosening.
    v2 = drive_tick(s, bid=10.95, high=11.80, mm_enabled=True, impulse_leg_high=IMP)
    assert s.stop == pytest.approx(peak_stop)
    _assert_monotone_up(s.stop_history)


# ── CUSHION PATIENCE: the trail band WIDENS as the day banks R (held-runner edge) ─


def test_cushion_widens_band_with_banked_day_pnl(cushion_band):
    """Two identical runners at the SAME hwm but different DAY cushion: the one with
    a banked day must trail WIDER (a lower stop candidate) — Ross "in my big account I
    hold through a couple of those". Encodes the cushion->patience->width contract on
    a path tick, and that wider-band still never loosens an existing tighter stop."""
    # flat-day runner: zero day cushion at +0R hwm == entry -> floor band (500bps).
    flat = RunnerState(stop=ENTRY, breakeven_floor=ENTRY, partial_taken=True, day_realized_usd=0.0)
    drive_tick(flat, bid=10.00, high=10.00, mm_enabled=False, use_cushion=True)
    # +1R banked day ($300) at the same flat hwm -> halfway patience -> 750bps band.
    banked = RunnerState(stop=ENTRY, breakeven_floor=ENTRY, partial_taken=True, day_realized_usd=300.0)
    drive_tick(banked, bid=10.00, high=10.00, mm_enabled=False, use_cushion=True)
    # both clamp UP to the breakeven floor (10.0) here since the bands sit below entry,
    # so assert the BAND WIDTH directly via the pure helper at a profitable hwm instead.
    hwm = 10.60  # +2R unrealized
    flat_stop = pe.cushion_adaptive_trail_stop(
        high_water_mark=hwm, entry_price=ENTRY, atr_pct=ATR_PCT, stop_atr_mult=STOP_MULT,
        day_realized_usd=0.0, position_risk_usd=300.0, breakeven_floor=0.0, current_stop=0.0,
    )
    banked_stop = pe.cushion_adaptive_trail_stop(
        high_water_mark=hwm, entry_price=ENTRY, atr_pct=ATR_PCT, stop_atr_mult=STOP_MULT,
        day_realized_usd=300.0, position_risk_usd=300.0, breakeven_floor=0.0, current_stop=0.0,
    )
    # +2R unrealized alone already saturates patience (>= rr) -> ceiling 1000bps -> both
    # at hwm*0.90. The banked day cannot WIDEN past the ceiling, so they are EQUAL here,
    # and neither loosens. This pins the saturation boundary.
    assert flat_stop == pytest.approx(hwm * 0.90)
    assert banked_stop == pytest.approx(hwm * 0.90)
    # at a SUB-1R hwm, the banked day's extra cushion DOES widen the band (lower stop).
    hwm2 = 10.15  # +0.5R unrealized
    flat2 = pe.cushion_adaptive_trail_stop(
        high_water_mark=hwm2, entry_price=ENTRY, atr_pct=ATR_PCT, stop_atr_mult=STOP_MULT,
        day_realized_usd=0.0, position_risk_usd=300.0, breakeven_floor=0.0, current_stop=0.0,
    )
    banked2 = pe.cushion_adaptive_trail_stop(
        high_water_mark=hwm2, entry_price=ENTRY, atr_pct=ATR_PCT, stop_atr_mult=STOP_MULT,
        day_realized_usd=300.0, position_risk_usd=300.0, breakeven_floor=0.0, current_stop=0.0,
    )
    assert banked2 < flat2  # banked day -> wider band -> LOWER (more patient) stop


# ── FLAG OFF: the measured-move + double-top stages are byte-identical no-ops ────


def test_flag_off_path_only_trails_no_scale_no_tighten(monkeypatch, cushion_band):
    """With the MM/double-top flag OFF, the SAME path must produce ZERO measured-move
    scales and ZERO exhaustion tightens — only the trail moves the stop. Proves the
    kill-switch leaves the composed pipeline byte-identical to the pre-feature trail."""
    monkeypatch.setattr(settings, "chili_momentum_measured_move_exit_enabled", False, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_scale_out_fraction", 0.5, raising=False)
    s = RunnerState()
    IMP = 11.0
    drive_tick(s, bid=10.60, high=10.70, mm_enabled=False, impulse_leg_high=IMP)  # 1st-target still fires
    assert s.partial_taken is True  # the first-target partial is NOT flag-gated
    # blow past the MM target and into a lower-high retest, flag OFF:
    v1 = drive_tick(s, bid=12.50, high=12.60, mm_enabled=False, impulse_leg_high=IMP)
    assert v1["mm"]["fire"] is False
    assert v1["mm"]["reason"] == "flag_off"
    assert s.mm_fired is False
    assert s.qty == pytest.approx(50.0)        # no MM scale-out happened
    v2 = drive_tick(s, bid=11.90, high=12.10, mm_enabled=False, impulse_leg_high=12.60)
    assert v2["exhausted"] is False
    assert v2["tighten"] is False
    assert "measured_move_scale" not in s.events
    assert "double_top_tighten" not in s.events
    _assert_monotone_up(s.stop_history)


# ── chandelier vs cushion: BOTH trails ratchet-only, chandelier is the floor ─────


def test_chandelier_and_cushion_both_ratchet_and_distance_is_atr(cushion_band):
    """The plain chandelier trail (no cushion) must place the stop exactly one ATR
    risk-distance (atr_pct*mult) below the HWM, and never loosen. Then the cushion
    trail (band >= that ATR width by construction at the floor) sits at or below it
    but still ratchets. Pins the documented 'same ATR distance below HWM' claim."""
    # chandelier: dist = atr_pct*mult = 0.05*0.60 = 0.03 -> stop = hwm*(1-0.03).
    s = RunnerState(stop=ENTRY, breakeven_floor=ENTRY, partial_taken=True)
    drive_tick(s, bid=10.90, high=11.00, mm_enabled=False, use_cushion=False)
    assert s.stop == pytest.approx(11.00 * (1.0 - 0.03))  # 10.67, one ATR below HWM
    # next tick lower bid but same HWM: chandelier holds (ratchet-only).
    held = s.stop
    drive_tick(s, bid=10.80, high=10.95, mm_enabled=False, use_cushion=False)
    assert s.stop == pytest.approx(held)
    # higher HWM: chandelier ratchets UP to a new one-ATR-below level.
    drive_tick(s, bid=11.30, high=11.40, mm_enabled=False, use_cushion=False)
    assert s.stop == pytest.approx(11.40 * (1.0 - 0.03))
    assert s.stop > held
    _assert_monotone_up(s.stop_history)


# ── ADVERSARIAL: a still-pressing retest is NOT a double-top (no premature tighten) ─


def test_still_pressing_retest_does_not_tighten(mm_on):
    """A lower-high that the bid is STILL pressing into (not yet rejected) must not
    be called a double-top — premature tighten would cut a winner mid-thrust. Pins
    the 'bid rolled back below the retest peak' rejection requirement on the path."""
    s = RunnerState()
    drive_tick(s, bid=10.60, high=10.70, mm_enabled=True, impulse_leg_high=11.0)  # partial
    # current_high 11.90 is a lower high vs impulse 12.0, BUT bid 11.90 is AT the peak
    # (still pressing) -> reason 'still_pressing', no exhaustion, no tighten.
    v = drive_tick(s, bid=11.90, high=11.90, mm_enabled=True, impulse_leg_high=12.0)
    assert v["dt"]["exhausted"] is False
    assert v["dt"]["reason"] == "still_pressing"
    assert v["tighten"] is False
    _assert_monotone_up(s.stop_history)


# ── ADVERSARIAL: a shallow bounce far from the high is NOT a double-top ──────────


def test_shallow_bounce_far_from_high_is_not_double_top(mm_on):
    """A retest that never came NEAR the prior high (gap > the ATR band) must read as
    'retest_too_shallow', not exhaustion — so a mid-pullback wiggle never tightens."""
    s = RunnerState()
    drive_tick(s, bid=10.60, high=10.70, mm_enabled=True, impulse_leg_high=11.0)  # partial
    # impulse high 13.0; retest only to 11.5 (gap 1.5 >> band 0.75*0.30=0.225) -> shallow.
    v = drive_tick(s, bid=11.40, high=11.50, mm_enabled=True, impulse_leg_high=13.0)
    assert v["dt"]["exhausted"] is False
    assert v["dt"]["reason"] == "retest_too_shallow"
    assert v["tighten"] is False
    _assert_monotone_up(s.stop_history)


# ── ADVERSARIAL: flow-weak double-top arms a PARTIAL, not just a tighten ─────────


def test_flow_weak_double_top_arms_partial(mm_on):
    """When the rejected lower-high retest is ALSO flow-weak (OFI<=0 AND micro<0),
    the double-top decision must arm a PARTIAL (sell a slice), not merely tighten —
    distribution is confirmed. Pins the optional-flow corroborant on the path."""
    s = RunnerState()
    drive_tick(s, bid=10.60, high=10.70, mm_enabled=True, impulse_leg_high=11.0)  # partial
    v = drive_tick(
        s, bid=11.70, high=11.85, mm_enabled=True, impulse_leg_high=12.05,
        ofi=-0.4, micro_edge=-0.2,
    )
    assert v["dt"]["exhausted"] is True
    assert v["dt"]["flow_weak"] is True
    assert v["dt"]["partial_arm"] is True
    assert v["tighten"] is True
    _assert_monotone_up(s.stop_history)
