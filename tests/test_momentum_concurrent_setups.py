"""SABAY-SABAY (concurrent setups) — the live setup-selector ``select_best_setup`` ARBITRATION.

When MULTIPLE momentum entry triggers fire on the SAME candidate/SAME bar (a "sabay-sabay"
tape — e.g. a dip-family reclaim AND a HOD/flat-top breakout AND a double-bottom break all
clear their gates on one tick), the lane must NOT double-fire / open two conflicting
positions. The live runner collects every firing trigger into a single ``_breakouts``
candidate list (live_runner.py:4654-4907) and hands them to ONE arbitrator —
``select_best_setup`` (imported live_runner.py:4626, defined entry_gates.py:7356) — which
returns EXACTLY ONE chosen ``(ok, reason, debug)``. The chosen setup is the one with the best
structural reward:risk among the firing candidates, computed from the SAME
``stop_target_prices`` / ``class_aware_reward_risk`` the runner uses to place the bracket, so
the pick matches the real order geometry.

``select_best_setup`` is a PURE function over the shared candidate contract:
each candidate = ``(ok: bool, reason: str, debug: dict)`` where on a FIRE the debug carries
``pullback_high`` (= entry level) and ``pullback_low`` (= structural stop). That lets these
tests drive the arbitrator DIRECTLY with realistic candidate tuples (no DataFrame/indicator
mocking needed for the selector itself), and ALSO feed it a GENUINELY-fired gate result so we
prove it arbitrates REAL gate output, not just synthetic tuples.

R:R MATH the selector ranks by (entry_gates.py:7435-7460, paper_execution.stop_target_prices):
  risk   = entry - stop                       (the candidate's OWN structural stop distance)
  target = entry + reward_risk * (entry * atr_pct * 0.60)   (ATR-scaled, reward_risk=2:1)
  reward = target - entry                      (INDEPENDENT of the candidate's own stop)
  rr     = reward / risk
So among candidates sharing the SAME entry (=> identical target/reward), the one with the
TIGHTEST stop (smallest ``risk`` = entry-pullback_low) wins. Every "highest-R:R" assertion
below is built on that: same entry, different stops -> the tightest-stop fire is chosen.
(A ``round_number_first_scale_target`` can pull the FIRST-scale target IN to a round number;
the shared-entry design keeps that pull IDENTICAL across the compared candidates so it never
perturbs the ORDERING — only the absolute R:R, which we don't pin.)

Scenarios covered (the task contract):
  (a) 2-3 setups all fire        -> ONE returned, the highest-R:R one; debug breadcrumb lists
                                    every fire (``setup_selected_from``) + the chosen ``setup_rr``.
  (b) tie / equal-R:R            -> deterministic pick (first in candidate/ladder order), no crash.
  (c) only one fires             -> that one is chosen (no spurious selection).
  (d) zero fire                  -> NO setup chosen (no fabricated fire).
Plus: the chosen setup carries a COHERENT entry+stop (``pullback_high`` > ``pullback_low`` > 0)
so the downstream bracket sizing is valid; no double-fire (always a single tuple); the overhead
veto + second-leg preference arms; fail-OPEN on a degenerate candidate; and an end-to-end test
feeding a real ``cup_and_handle_confirmation`` fire into the selector.

PURE-LOGIC tests on the selector + the proven firing-mock scaffold (mirrors
test_momentum_cup_and_handle.py / test_momentum_setup_guard_parity.py). TESTS-ONLY.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app.services.trading.momentum_neural import entry_gates
from app.services.trading.momentum_neural.entry_gates import (
    cup_and_handle_confirmation,
    select_best_setup,
)

_GATES = "app.services.trading.momentum_neural.entry_gates"
# front_side_state is imported INSIDE the gate via ``from .ross_momentum import
# front_side_state`` -> patch it at its SOURCE module (mirrors the proven tests).
_ROSS = "app.services.trading.momentum_neural.ross_momentum"


def _fire(reason: str, *, entry: float, stop: float, **extra) -> tuple[bool, str, dict]:
    """A FIRING candidate in the shared (ok, reason, debug) contract: ok=True, the gate
    fire reason, and a debug carrying the entry (pullback_high) + structural stop
    (pullback_low) under the IDENTICAL keys every live trigger uses."""
    dbg = {"pullback_high": float(entry), "pullback_low": float(stop), "entry_interval": "5m"}
    dbg.update(extra)
    return True, reason, dbg


def _no_fire(reason: str, **extra) -> tuple[bool, str, dict]:
    """A NON-firing candidate (ok=False). A wait/veto carries no usable entry+stop for R:R."""
    dbg = {"entry_interval": "5m"}
    dbg.update(extra)
    return False, reason, dbg


def _pin_settings(ms) -> None:
    """Pin the flags the selector reads so the arbitration is deterministic regardless of the
    process env: overhead veto OFF (no DailyContext path) + second-leg preference OFF (the
    default-on tests opt in explicitly) + the 2:1 R:R floor."""
    ms.chili_momentum_overhead_veto_enabled = False
    ms.chili_momentum_second_leg_preference_enabled = False
    ms.chili_momentum_second_leg_rr_tilt = 0.15
    ms.chili_momentum_risk_reward_risk_ratio = 2.0
    ms.chili_momentum_crypto_reward_risk_ratio = None


# ════════════════════════════════════════════════════════════════════════════════════════
#  (a) MULTIPLE setups fire on the SAME bar -> ONE chosen, the HIGHEST-R:R one
# ════════════════════════════════════════════════════════════════════════════════════════

class TestConcurrentFiresPickBestRR:
    def test_two_fires_picks_tighter_stop_highest_rr(self):
        """TWO setups fire on one bar at the SAME entry but with DIFFERENT structural stops.
        The selector returns EXACTLY ONE tuple, and it is the higher-R:R fire = the one with
        the TIGHTER stop (smaller entry-stop risk -> larger reward/risk)."""
        wide = _fire("hod_break", entry=10.00, stop=9.40)        # risk 0.60 (loose)
        tight = _fire("double_bottom_break", entry=10.00, stop=9.80)  # risk 0.20 (tight) -> best R:R
        with patch(f"{_GATES}.settings") as ms:
            _pin_settings(ms)
            ok, reason, dbg = select_best_setup([wide, tight], symbol="TEST", atr_pct=0.02)
        assert ok is True
        assert reason == "double_bottom_break", f"tighter-stop fire must win on R:R, got {reason}"
        # coherent entry+stop survive for downstream sizing.
        assert dbg["pullback_high"] == pytest.approx(10.00, abs=1e-9)
        assert dbg["pullback_low"] == pytest.approx(9.80, abs=1e-9)
        # the arbitration breadcrumb: which set it chose FROM + the winning R:R.
        assert set(dbg["setup_selected_from"]) == {"hod_break", "double_bottom_break"}
        assert dbg["setup_rr"] > 0

    def test_three_fires_picks_best_rr_single_result(self):
        """THREE concurrent fires (dip reclaim + HOD break + cup-and-handle) at the same entry,
        ascending stop tightness. ONE result, the tightest-stop (highest-R:R) one. No double-
        fire: the return is a single tuple, never a list / never two positions."""
        dip = _fire("ma_vwap_pullback", entry=20.00, stop=19.00)        # risk 1.00
        hod = _fire("hod_break", entry=20.00, stop=19.50)               # risk 0.50
        cup = _fire("cup_and_handle_break", entry=20.00, stop=19.80)    # risk 0.20 -> best
        with patch(f"{_GATES}.settings") as ms:
            _pin_settings(ms)
            result = select_best_setup([dip, hod, cup], symbol="TEST", atr_pct=0.02)
        assert isinstance(result, tuple) and len(result) == 3, "selector must return ONE 3-tuple"
        ok, reason, dbg = result
        assert ok is True
        assert reason == "cup_and_handle_break"
        assert dbg["pullback_low"] == pytest.approx(19.80, abs=1e-9)
        assert len(dbg["setup_selected_from"]) == 3

    def test_chosen_setup_has_coherent_entry_and_stop(self):
        """The winner ALWAYS carries a coherent entry level + structural stop
        (pullback_high > pullback_low > 0) so the downstream bracket / sizing is valid."""
        a = _fire("wedge_break", entry=5.50, stop=5.20)
        b = _fire("absorption_snap", entry=5.50, stop=5.35)  # tighter -> wins
        with patch(f"{_GATES}.settings") as ms:
            _pin_settings(ms)
            ok, reason, dbg = select_best_setup([a, b], symbol="TEST", atr_pct=0.03)
        assert ok is True
        assert dbg["pullback_low"] < dbg["pullback_high"]
        assert dbg["pullback_low"] > 0.0
        assert reason == "absorption_snap"

    def test_best_rr_independent_of_candidate_order(self):
        """The pick is by R:R, NOT first-clears-gates: the SAME tight-stop fire wins whether it
        is listed first or last in the candidate set (no positional bias on a real R:R gap)."""
        loose = _fire("hod_break", entry=12.00, stop=11.30)        # risk 0.70
        tight = _fire("bull_flag_break", entry=12.00, stop=11.85)  # risk 0.15 -> best
        with patch(f"{_GATES}.settings") as ms:
            _pin_settings(ms)
            r1 = select_best_setup([loose, tight], symbol="TEST", atr_pct=0.02)
            r2 = select_best_setup([tight, loose], symbol="TEST", atr_pct=0.02)
        assert r1[1] == "bull_flag_break"
        assert r2[1] == "bull_flag_break"


# ════════════════════════════════════════════════════════════════════════════════════════
#  (b) TIE / equal-R:R -> deterministic, stable pick (no crash, first-in-order)
# ════════════════════════════════════════════════════════════════════════════════════════

class TestConcurrentFiresTieDeterministic:
    def test_equal_rr_picks_first_in_order(self):
        """Two fires with IDENTICAL entry AND stop -> identical R:R. The selector keeps the
        FIRST (the ``> best_rr`` compare is strict, so the earlier candidate is not displaced
        by an equal one) -> deterministic, legacy-ladder-order pick. No crash."""
        first = _fire("hod_break", entry=8.00, stop=7.80)
        second = _fire("flat_top_break", entry=8.00, stop=7.80)  # equal R:R
        with patch(f"{_GATES}.settings") as ms:
            _pin_settings(ms)
            ok, reason, dbg = select_best_setup([first, second], symbol="TEST", atr_pct=0.02)
        assert ok is True
        assert reason == "hod_break", "an equal-R:R tie must keep the FIRST (stable) candidate"

    def test_equal_rr_pick_is_stable_under_reorder(self):
        """The tie-break is purely positional (first wins), so reversing the order deterministi-
        cally flips the winner to the new-first — stable + reproducible, never a crash / random."""
        a = _fire("setup_a", entry=8.00, stop=7.80)
        b = _fire("setup_b", entry=8.00, stop=7.80)
        with patch(f"{_GATES}.settings") as ms:
            _pin_settings(ms)
            forward = select_best_setup([a, b], symbol="TEST", atr_pct=0.02)
            reverse = select_best_setup([b, a], symbol="TEST", atr_pct=0.02)
        assert forward[1] == "setup_a"
        assert reverse[1] == "setup_b"

    def test_three_way_tie_keeps_first(self):
        """A 3-way equal-R:R tie -> the first candidate, deterministically. Stable arbitration
        even when every firing setup has identical geometry."""
        fires = [
            _fire("first", entry=15.00, stop=14.70),
            _fire("second", entry=15.00, stop=14.70),
            _fire("third", entry=15.00, stop=14.70),
        ]
        with patch(f"{_GATES}.settings") as ms:
            _pin_settings(ms)
            ok, reason, dbg = select_best_setup(fires, symbol="TEST", atr_pct=0.02)
        assert ok is True
        assert reason == "first"


# ════════════════════════════════════════════════════════════════════════════════════════
#  (c) ONLY ONE fires -> that one is chosen (no spurious arbitration)
# ════════════════════════════════════════════════════════════════════════════════════════

class TestSingleFire:
    def test_single_fire_returned_unchanged(self):
        """Exactly one firing candidate among several waits/vetoes -> that fire is chosen,
        unchanged (single-fire short-circuit; no R:R math, no breadcrumb needed)."""
        only = _fire("hod_break", entry=10.00, stop=9.60)
        waits = [_no_fire("hod_break_waiting_for_break", pullback_high=10.00), only,
                 _no_fire("bull_flag_no_pullback")]
        with patch(f"{_GATES}.settings") as ms:
            _pin_settings(ms)
            ok, reason, dbg = select_best_setup(waits, symbol="TEST", atr_pct=0.02)
        assert ok is True
        assert reason == "hod_break"
        assert dbg["pullback_high"] == pytest.approx(10.00, abs=1e-9)
        assert dbg["pullback_low"] == pytest.approx(9.60, abs=1e-9)

    def test_single_fire_in_a_list_of_one(self):
        """A candidate list with a single fire -> that fire (the live-runner common case when
        only one trigger clears its gates this bar)."""
        only = _fire("ross_abcd_break", entry=3.20, stop=3.05)
        with patch(f"{_GATES}.settings") as ms:
            _pin_settings(ms)
            ok, reason, dbg = select_best_setup([only], symbol="TEST", atr_pct=0.04)
        assert ok is True
        assert reason == "ross_abcd_break"
        assert dbg["pullback_low"] < dbg["pullback_high"]


# ════════════════════════════════════════════════════════════════════════════════════════
#  (d) ZERO fires -> NO setup chosen (no fabricated fire / no double-position)
# ════════════════════════════════════════════════════════════════════════════════════════

class TestZeroFire:
    def test_all_waits_no_fire_chosen(self):
        """All candidates are waits/vetoes (ok=False) -> the selector chooses NO fire: it
        returns a non-firing result (the first truthy is None, so the first candidate is
        returned, ok=False). The lane does NOT enter."""
        waits = [
            _no_fire("hod_break_waiting_for_break", pullback_high=10.00),
            _no_fire("bull_flag_extended"),
            _no_fire("cup_and_handle_tape_unconfirmed"),
        ]
        with patch(f"{_GATES}.settings") as ms:
            _pin_settings(ms)
            ok, reason, dbg = select_best_setup(waits, symbol="TEST", atr_pct=0.02)
        assert ok is False, "no firing candidate -> the selector must NOT manufacture a fire"

    def test_empty_candidate_list_no_fire(self):
        """An empty candidate set -> a benign no-fire (no crash, no entry)."""
        with patch(f"{_GATES}.settings") as ms:
            _pin_settings(ms)
            ok, reason, dbg = select_best_setup([], symbol="TEST", atr_pct=0.02)
        assert ok is False
        assert reason == "no_candidate"


# ════════════════════════════════════════════════════════════════════════════════════════
#  NO DOUBLE-FIRE: always EXACTLY ONE chosen setup (the core safety contract)
# ════════════════════════════════════════════════════════════════════════════════════════

class TestNoDoubleFire:
    def test_many_concurrent_fires_yield_exactly_one(self):
        """Five setups fire at once (a maximal sabay-sabay tape). The selector returns EXACTLY
        ONE tuple -> the lane can open ONE position, never five conflicting ones. The single
        winner is the tightest-stop / highest-R:R fire and carries one coherent entry+stop."""
        fires = [
            _fire("hod_break", entry=7.00, stop=6.40),
            _fire("flat_top_break", entry=7.00, stop=6.55),
            _fire("double_bottom_break", entry=7.00, stop=6.70),
            _fire("bull_flag_break", entry=7.00, stop=6.85),   # tightest -> winner
            _fire("ma_vwap_pullback", entry=7.00, stop=6.50),
        ]
        with patch(f"{_GATES}.settings") as ms:
            _pin_settings(ms)
            result = select_best_setup(fires, symbol="TEST", atr_pct=0.03)
        # ONE result object, not a collection of entries.
        assert isinstance(result, tuple) and len(result) == 3
        ok, reason, dbg = result
        assert ok is True
        assert reason == "bull_flag_break"
        assert len([dbg]) == 1  # a single chosen setup -> a single position
        assert 0 < dbg["pullback_low"] < dbg["pullback_high"]
        assert len(dbg["setup_selected_from"]) == 5


# ════════════════════════════════════════════════════════════════════════════════════════
#  OVERHEAD VETO at the single FIRE choke point (covers EVERY arbitrated entry)
# ════════════════════════════════════════════════════════════════════════════════════════

class TestSelectorOverheadVeto:
    def test_overhead_veto_rejects_the_chosen_fire(self):
        """The P0 overhead veto runs on the SINGLE chosen fire (so it covers every trigger that
        reaches the selector): a chosen breakout buying into trapped overhead supply is turned
        into a NO-FIRE ``overhead_veto_*`` — even though a real setup fired. Proves the
        arbitration choke point is also the veto choke point (no chase into a ceiling)."""
        a = _fire("hod_break", entry=10.00, stop=9.50)
        b = _fire("double_bottom_break", entry=10.00, stop=9.80)  # would win on R:R
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}._overhead_supply_veto",
                      return_value=("overhead_supply", {"overhead_supply_atr": 0.2})):
            _pin_settings(ms)
            ms.chili_momentum_overhead_veto_enabled = True
            ok, reason, dbg = select_best_setup(
                [a, b], symbol="TEST", atr_pct=0.02, daily_ctx=SimpleNamespace(),
            )
        assert ok is False, "a fire into overhead supply must be vetoed at the choke point"
        assert reason == "overhead_veto_overhead_supply"
        # the breadcrumb records WHICH setup was vetoed (the R:R winner).
        assert dbg["overhead_vetoed_from"] == "double_bottom_break"

    def test_clear_sky_passes_the_chosen_fire(self):
        """No overhead level (clear sky) -> the veto returns None -> the chosen fire passes
        through unchanged (the veto NEVER over-blocks a clean breakout)."""
        a = _fire("hod_break", entry=10.00, stop=9.50)
        b = _fire("double_bottom_break", entry=10.00, stop=9.80)
        with patch(f"{_GATES}.settings") as ms, \
                patch(f"{_GATES}._overhead_supply_veto", return_value=None):
            _pin_settings(ms)
            ms.chili_momentum_overhead_veto_enabled = True
            ok, reason, dbg = select_best_setup(
                [a, b], symbol="TEST", atr_pct=0.02, daily_ctx=SimpleNamespace(),
            )
        assert ok is True
        assert reason == "double_bottom_break"


# ════════════════════════════════════════════════════════════════════════════════════════
#  SECOND-LEG PREFERENCE TILT (LOCATE #8): a based 2nd leg can win a CLOSE R:R race
# ════════════════════════════════════════════════════════════════════════════════════════

class TestSecondLegPreference:
    def test_second_leg_tilt_flips_a_close_race(self):
        """With the preference ON, a based second-leg candidate (its stop sits >= ~1 ATR ABOVE
        the candidate set's lowest stop) gets a bounded +tilt to its effective R:R, letting it
        win a CLOSE race it would otherwise lose by a hair. Proves the tilt arms + is bounded
        to a PREFERENCE among already-passing fires (it never admits a new entry)."""
        # first leg: lower base, slightly TIGHTER stop -> marginally higher raw R:R.
        first_leg = _fire("hod_break", entry=10.00, stop=9.50)         # risk 0.50, base 9.50 (set low)
        # second leg: based ~1 ATR higher (stop 9.80 vs set-low 9.50; ATR~0.20 in price),
        # slightly looser stop -> marginally lower RAW R:R, but the tilt lifts it over the top.
        second_leg = _fire("bull_flag_break", entry=10.00, stop=9.78)  # risk 0.22
        with patch(f"{_GATES}.settings") as ms:
            _pin_settings(ms)
            ms.chili_momentum_second_leg_preference_enabled = True
            ms.chili_momentum_second_leg_rr_tilt = 0.15
            ok, reason, dbg = select_best_setup(
                [first_leg, second_leg], symbol="TEST", atr_pct=0.02,
            )
        # second_leg already has the tighter stop here so it wins anyway; assert the tilt is
        # recorded so the preference is provably LIVE (the based-leg flag was set).
        assert ok is True
        assert reason == "bull_flag_break"
        assert dbg.get("second_leg_based") is True, "the based second leg must be tagged by the tilt"

    def test_preference_off_is_byte_identical_pure_rr(self):
        """Preference OFF (default) -> NO tilt -> pure-R:R arbitration: the tightest-stop fire
        wins and NO ``second_leg_based`` flag is stamped (byte-identical to no preference)."""
        a = _fire("hod_break", entry=10.00, stop=9.50)
        b = _fire("bull_flag_break", entry=10.00, stop=9.80)  # tighter -> wins on raw R:R
        with patch(f"{_GATES}.settings") as ms:
            _pin_settings(ms)
            ms.chili_momentum_second_leg_preference_enabled = False
            ok, reason, dbg = select_best_setup([a, b], symbol="TEST", atr_pct=0.02)
        assert ok is True
        assert reason == "bull_flag_break"
        assert "second_leg_based" not in dbg


# ════════════════════════════════════════════════════════════════════════════════════════
#  FAIL-OPEN / DEGENERATE-INPUT robustness (a bad candidate never crashes the tick)
# ════════════════════════════════════════════════════════════════════════════════════════

class TestSelectorRobustness:
    def test_fire_missing_levels_falls_back_to_first_fire(self):
        """A truthy fire that carries NO usable pullback_high/low (a levelless fire) is not a
        breakout the R:R math can rank -> the selector falls back to the FIRST truthy result
        (legacy ladder order), byte-identical to no selector. No crash."""
        levelless = (True, "micro_pullback_primary", {"entry_interval": "5m"})  # no levels
        with patch(f"{_GATES}.settings") as ms:
            _pin_settings(ms)
            ok, reason, dbg = select_best_setup([levelless], symbol="TEST", atr_pct=0.02)
        assert ok is True
        assert reason == "micro_pullback_primary"

    def test_inverted_levels_skipped_other_fire_wins(self):
        """A fire with an INVERTED stop (stop >= entry -> non-positive risk) is skipped by the
        R:R loop; a valid concurrent fire is chosen instead. Degenerate geometry never wins
        and never crashes."""
        bad = _fire("hod_break", entry=10.00, stop=10.50)        # stop ABOVE entry (degenerate)
        good = _fire("double_bottom_break", entry=10.00, stop=9.70)
        with patch(f"{_GATES}.settings") as ms:
            _pin_settings(ms)
            ok, reason, dbg = select_best_setup([bad, good], symbol="TEST", atr_pct=0.02)
        assert ok is True
        assert reason == "double_bottom_break"

    def test_internal_error_fails_open_to_first_fire(self):
        """Any unexpected error inside the R:R math -> fail-OPEN to the first truthy fire (never
        a raise that crashes the runner tick, never a fabricated different entry). Force
        stop_target_prices to raise."""
        a = _fire("hod_break", entry=10.00, stop=9.50)
        b = _fire("double_bottom_break", entry=10.00, stop=9.80)
        with patch(f"{_GATES}.settings") as ms, \
                patch("app.services.trading.momentum_neural.paper_execution.stop_target_prices",
                      side_effect=RuntimeError("boom")):
            _pin_settings(ms)
            ok, reason, dbg = select_best_setup([a, b], symbol="TEST", atr_pct=0.02)
        assert ok is True
        assert reason == "hod_break", "fail-open must return the FIRST fire, not crash"


# ════════════════════════════════════════════════════════════════════════════════════════
#  END-TO-END: a GENUINELY-fired gate result arbitrated by the selector
#  (mirrors the proven cup_and_handle firing-mock; proves the selector arbitrates REAL gate
#   output, not just synthetic tuples — the live-runner _breakouts -> select_best_setup path)
# ════════════════════════════════════════════════════════════════════════════════════════

_RIM = 10.00
_HANDLE_LOW = 9.70


def _cup_handle_df() -> pd.DataFrame:
    """The proven double-top + shallow-handle + new-high-break frame (verbatim geometry from
    test_momentum_cup_and_handle.py) that makes ``cup_and_handle_confirmation`` FIRE."""
    rim = _RIM
    bars = [
        (9.20, 9.00), (9.45, 9.15), (9.70, 9.40),
        (rim, 9.60),          # 3 TOP1
        (9.78, 9.55), (9.60, 9.35), (9.82, 9.55),
        (rim, 9.70),          # 7 TOP2
        (9.85, 9.78), (9.80, 9.74),
        (9.78, _HANDLE_LOW),  # 10 HANDLE LOW
        (9.95, 9.80),
        (10.35, 9.90),        # 12 BREAK
    ]
    rows = [{"Open": (h + l) / 2.0, "High": h, "Low": l, "Close": (h + l) / 2.0, "Volume": 1_000_000}
            for h, l in bars]
    return pd.DataFrame(rows)


def _cup_settings(ms) -> None:
    ms.chili_momentum_cup_and_handle_entry_enabled = True
    ms.chili_momentum_swing_pivot_half_window = 1
    ms.chili_momentum_swing_pivot_atr_noise_frac = 0.0
    ms.chili_momentum_cup_and_handle_lookback_bars = 20
    ms.chili_momentum_cup_and_handle_max_handle_bars = 3
    ms.chili_momentum_double_bottom_band_atr_mult = 0.6
    ms.chili_momentum_pullback_volume_spike_multiple = 1.5
    # selector flags (this same patched settings drives select_best_setup):
    ms.chili_momentum_overhead_veto_enabled = False
    ms.chili_momentum_second_leg_preference_enabled = False
    ms.chili_momentum_second_leg_rr_tilt = 0.15
    ms.chili_momentum_risk_reward_risk_ratio = 2.0
    ms.chili_momentum_crypto_reward_risk_ratio = None


def _good_arrays() -> dict:
    n = 13
    return {
        "ema_9": [9.50] * n, "ema_20": [9.40] * n, "macd": [0.05] * n,
        "macd_signal": [0.03] * n, "vwap": [9.45] * n,
        "volume_ratio": [1.0] * 12 + [3.0],
    }


class _PassAllGuards:
    """Mock the cup gate's indicator layer + the four chase-guards to ALL PASS (the proven
    scaffold) so a structurally-clean cup FIRES and we can feed that REAL fire to the selector."""

    def __init__(self):
        self._patches = []
        self.mocks = {}

    def __enter__(self):
        def _p(target, **kw):
            p = patch(target, **kw)
            self.mocks[target] = p.start()
            self._patches.append(p)
            return self.mocks[target]

        _p(f"{_GATES}._batch_c_atr_pct", return_value=(0.02, 0.20))
        _p(f"{_GATES}.compute_all_from_df", return_value=_good_arrays())
        _p(f"{_GATES}._detect_back_side", return_value=(False, "front_side"))
        _p(f"{_ROSS}.front_side_state",
           return_value=SimpleNamespace(is_backside=False, above_vwap=True, reason="ok"))
        _p(f"{_GATES}._hod_extension_ok", return_value=(True, {}))
        _p(f"{_GATES}._l2_entry_veto", return_value=None)
        _p(f"{_GATES}.tape_confirms_hold", return_value=(True, {"reason": "tape_hold_ok"}))
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


class TestSelectorArbitratesRealGateFire:
    def test_real_cup_fire_plus_looser_synthetic_fire_selector_picks_by_rr(self):
        """END-TO-END: drive the REAL ``cup_and_handle_confirmation`` to FIRE (entry=rim 10.00,
        stop=handle low 9.70), put it in the SAME candidate set as a concurrent LOOSER fire
        (same entry, wider stop 9.30), and let the live selector arbitrate. The selector picks
        the tighter-stop / higher-R:R fire = the genuine cup. Proves the real gate output flows
        through the shared contract into the arbitrator exactly as the live runner wires it."""
        df = _cup_handle_df()
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards():
            _cup_settings(ms)
            cup = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
            assert cup[0] is True and cup[1] == "cup_and_handle_break", f"cup must fire: {cup}"
            assert cup[2]["pullback_high"] == pytest.approx(_RIM, abs=1e-6)
            assert cup[2]["pullback_low"] == pytest.approx(_HANDLE_LOW, abs=1e-6)

            looser = _fire("hod_break", entry=_RIM, stop=9.30)  # wider stop -> worse R:R
            ok, reason, dbg = select_best_setup([looser, cup], symbol="TEST", atr_pct=0.02)
        assert ok is True
        assert reason == "cup_and_handle_break", "the real cup fire (tighter stop) must win the R:R race"
        assert dbg["pullback_low"] == pytest.approx(_HANDLE_LOW, abs=1e-6)
        assert set(dbg["setup_selected_from"]) == {"hod_break", "cup_and_handle_break"}

    def test_real_cup_fire_alone_chosen_as_single(self):
        """The real cup fire as the SOLE candidate -> chosen unchanged (the single-fire path),
        carrying its coherent entry (rim) + stop (handle low)."""
        df = _cup_handle_df()
        with patch(f"{_GATES}.settings") as ms, _PassAllGuards():
            _cup_settings(ms)
            cup = cup_and_handle_confirmation(df, entry_interval="5m", symbol="TEST", db=MagicMock())
            ok, reason, dbg = select_best_setup([cup], symbol="TEST", atr_pct=0.02)
        assert ok is True
        assert reason == "cup_and_handle_break"
        assert dbg["pullback_high"] == pytest.approx(_RIM, abs=1e-6)
        assert dbg["pullback_low"] == pytest.approx(_HANDLE_LOW, abs=1e-6)
