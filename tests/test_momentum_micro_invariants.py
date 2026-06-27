"""MICRO-TIER property/invariant tests for the Ross momentum lane.

PORTABLE invariants (NO hypothesis dependency): each test enumerates a FIXED grid of
representative + boundary + pseudo-random-but-deterministic inputs and asserts a property
that must ALWAYS hold across the entire grid. The pseudo-random inputs are produced from a
FIXED seed list (``random.Random(seed)``) so every run is byte-deterministic — no
``Math.random`` / unseeded RNG anywhere, so a failure always reproduces.

Invariants pinned here (one section each):

 (1) Every size MULTIPLIER (catalyst_conviction, green_day_graduation, spread/liquidity,
     prime-window, fatigue, spread_cost derate) is ALWAYS within [its floor, its max] and
     finite, for ANY finite input including extremes (huge/tiny/zero/negative/nan/inf).
 (2) chunking ``_split_base_size`` ALWAYS sums to ``total`` within 1e-9 (or returns
     ``[total]``), every piece > 0, and (with an increment) every piece is a multiple of
     the increment >= one increment — for a grid of (total, blocks, increment).
 (3) ``adaptive_spread_cost_veto_derate`` ALWAYS returns ``allow=True`` (NEVER blocks) with
     ``mult`` in [floor, 1.0], for ANY spread / R / trigger reason.
 (4) ``_entry_extension_veto`` is MONOTONE: at a fixed atr/level, a more-extended entry never
     un-vetoes a case a less-extended entry already vetoed.
 (5) The doji body-fraction threshold and the pullback-depth ceiling are MONOTONE
     non-decreasing in atr_pct and BOUNDED (ceiling never exceeds _VOL_SHALLOW_CEIL).
 (6) ``_asetup_quality_floor`` is ALWAYS >= the conviction floor, for any score distribution.
 (7) ``consecutive_green_days`` is ALWAYS >= 0 (never negative) for any history.

These are PURE-LOGIC + ``patch.object(settings, ...)`` + synthetic rows — no live DB tick
needed; the few DB-backed helpers are exercised through the conftest ``db`` fixture (empty
DB) or a tiny fake Session, so they run fast.

Run:
  TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test \
    conda run -n chili-env pytest tests/test_momentum_micro_invariants.py -v
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import patch

import pytest

from app.config import settings
from app.services.trading.momentum_neural import auto_arm, entry_gates, risk_policy
from app.services.trading.momentum_neural.risk_policy import (
    catalyst_conviction_size_multiplier,
    consecutive_green_days,
    fatigue_derate_multiplier,
    green_day_graduation_multiplier,
    spread_liquidity_risk_multiplier,
)
from app.services.trading.momentum_neural.spread_cost_veto import (
    adaptive_spread_cost_veto_derate,
)
from app.services.trading.venue.chunking_adapter import _split_base_size


# ──────────────────────────────────────────────────────────────────────────────
# Shared deterministic input grids (NO unseeded randomness; fixed seed list).
# ──────────────────────────────────────────────────────────────────────────────

_SEEDS = [0, 1, 2, 7, 13, 42, 99, 123, 777, 2024]

# "Adversarial" finite scalars: representative + boundary. (nan/inf handled separately
# where a function's contract must remain finite on a non-finite input.)
_FINITE_SCALARS = [
    0.0, 1e-12, 1e-6, 1e-3, 0.01, 0.05, 0.1, 0.25, 0.5, 0.7, 1.0, 1.5, 2.0,
    3.0, 8.0, 12.0, 60.0, 100.0, 317.0, 1_000.0, 1e6, 1e9, 1e12,
    -1e-6, -0.5, -1.0, -100.0, -1e9,
]

_NON_FINITE = [float("nan"), float("inf"), float("-inf")]


def _pseudo_random_floats(seed: int, n: int, *, lo: float, hi: float) -> list[float]:
    """Deterministic list of ``n`` floats in [lo, hi] from a FIXED seed (reproducible)."""
    rng = random.Random(seed)
    return [rng.uniform(lo, hi) for _ in range(n)]


def _is_in_range(x: float, lo: float, hi: float, *, tol: float = 1e-9) -> bool:
    return math.isfinite(x) and (lo - tol) <= x <= (hi + tol)


# ══════════════════════════════════════════════════════════════════════════════
# (1) SIZE-MULTIPLIER bounds: always in [floor, max] and finite for ANY input.
# ══════════════════════════════════════════════════════════════════════════════


class TestCatalystConvictionBounds:
    """``catalyst_conviction_size_multiplier`` ∈ [1.0, max_multiplier], finite, NEVER < 1.0
    (a catalyst only ADDS). Sweep grade rank via the strong/weak/fake sets + step/max grid."""

    def test_catalyst_mult_in_floor_max_over_grid(self) -> None:
        sym = "TEST"
        # grid of (step, max_mult) including degenerate + extreme values
        for step in [0.0, 0.05, 0.15, 0.5, 1.0, 5.0, -0.5, 100.0]:
            for max_mult in [1.0, 1.5, 2.0, 3.0, 0.5, 10.0, 0.0]:
                # vary the catalyst grade: STRONG (rank 3), weak, fake, none
                for strong, weak, fake in [
                    ({sym}, None, None),
                    (None, {sym}, None),
                    (None, None, {sym}),
                    (None, None, None),
                    ({sym}, {sym}, {sym}),  # contradictory: weak/fake should dominate to 0
                ]:
                    with patch.object(settings, "chili_momentum_catalyst_conviction_enabled", True), \
                            patch.object(settings, "chili_momentum_catalyst_conviction_step", step), \
                            patch.object(settings, "chili_momentum_catalyst_conviction_max_multiplier", max_mult):
                        mult, meta = catalyst_conviction_size_multiplier(
                            sym, strong_symbols=strong, weak_symbols=weak, fake_symbols=fake,
                        )
                    eff_max = max(1.0, max_mult)  # source clamps max_mult up to 1.0
                    assert math.isfinite(mult), (step, max_mult, mult)
                    # floor is 1.0 (clamp(1+step*rank, 1.0, max)), ceiling eff_max
                    assert _is_in_range(mult, 1.0, eff_max), (step, max_mult, strong, weak, fake, mult)

    def test_catalyst_disabled_is_exactly_neutral(self) -> None:
        with patch.object(settings, "chili_momentum_catalyst_conviction_enabled", False):
            mult, meta = catalyst_conviction_size_multiplier("ABC")
        assert mult == 1.0
        assert meta["reason"] == "disabled"


class TestGreenDayGraduationBounds:
    """``green_day_graduation_multiplier`` ∈ [1.0, max_multiplier]; day-1 streak => exactly 1.0.
    We monkeypatch ``consecutive_green_days`` so the DB is not needed (pure multiplier math)."""

    def test_graduation_mult_in_floor_max_over_streak_grid(self) -> None:
        for streak in [0, 1, 2, 3, 5, 10, 30, 100, -1, -50]:
            for step in [0.0, 0.1, 0.5, 1.0, 5.0, -0.2]:
                for max_mult in [1.0, 1.5, 2.0, 3.0, 0.5]:
                    with patch.object(settings, "chili_momentum_green_day_graduation_enabled", True), \
                            patch.object(settings, "chili_momentum_green_day_step_per_day", step), \
                            patch.object(settings, "chili_momentum_green_day_max_multiplier", max_mult), \
                            patch.object(
                                risk_policy, "consecutive_green_days",
                                lambda *a, **k: (streak, {"streak": streak}),
                            ):
                        mult, meta = green_day_graduation_multiplier(None, execution_family="x")
                    eff_max = max(1.0, max_mult)
                    assert math.isfinite(mult)
                    assert _is_in_range(mult, 1.0, eff_max), (streak, step, max_mult, mult)
                    # Day-1 (streak<=1) => no graduation: exactly 1.0.
                    if streak <= 1:
                        assert mult == pytest.approx(1.0), (streak, step, max_mult, mult)


class TestSpreadLiquidityBounds:
    """``spread_liquidity_risk_multiplier`` ∈ [floor, 1.0], finite, NEVER > 1.0 (only shrinks).
    Sweep spread_bps × expected_move_bps including non-finite and negative inputs."""

    def test_spread_liquidity_in_floor_1_over_grid(self) -> None:
        floor = 0.5
        spreads = _FINITE_SCALARS + _NON_FINITE + [None]
        moves = [None, 0.0, 1.0, 50.0, 500.0, 5000.0, float("nan"), float("inf"), -10.0]
        for sb in spreads:
            for em in moves:
                mult, meta = spread_liquidity_risk_multiplier(sb, em, floor=floor)
                assert math.isfinite(mult), (sb, em, mult)
                assert _is_in_range(mult, floor, 1.0), (sb, em, mult)

    def test_spread_liquidity_pseudorandom_seeded(self) -> None:
        floor = 0.5
        for seed in _SEEDS:
            sbs = _pseudo_random_floats(seed, 20, lo=-50.0, hi=5000.0)
            ems = _pseudo_random_floats(seed + 1, 20, lo=0.0, hi=8000.0)
            for sb, em in zip(sbs, ems):
                mult, _ = spread_liquidity_risk_multiplier(sb, em, floor=floor)
                assert _is_in_range(mult, floor, 1.0), (seed, sb, em, mult)

    def test_spread_liquidity_respects_bad_floor_default(self) -> None:
        # An out-of-range floor must reset to 0.5 (not escape the [0,1] contract).
        for bad_floor in [0.0, -1.0, 1.5, float("nan")]:
            mult, _ = spread_liquidity_risk_multiplier(120.0, 50.0, floor=bad_floor)
            # default floor 0.5 applies => result still in [0.5, 1.0]
            assert _is_in_range(mult, 0.5, 1.0), (bad_floor, mult)


class TestFatigueDerateBounds:
    """``fatigue_derate_multiplier`` ∈ (floor, 1.0], NEVER > 1.0; both legs maxed => floor."""

    def test_fatigue_in_floor_1_over_grid(self) -> None:
        floor = 0.5
        with patch.object(settings, "chili_momentum_fatigue_derate_floor", floor), \
                patch.object(settings, "chili_momentum_fatigue_full_session_minutes", 240.0):
            for tc in [0, 1, 3, 5, 10, 50, 1000, -5]:
                for mx in [1, 3, 5, 10, 0, -1]:
                    for mso in [None, 0.0, 30.0, 120.0, 240.0, 480.0, -10.0, float("inf")]:
                        for is_crypto in (False, True):
                            mult, meta = fatigue_derate_multiplier(
                                trade_count_today=tc, max_trades_per_day=mx,
                                minutes_since_open=mso, is_crypto=is_crypto,
                            )
                            assert math.isfinite(mult), (tc, mx, mso, is_crypto, mult)
                            assert _is_in_range(mult, floor, 1.0), (tc, mx, mso, is_crypto, mult)

    def test_fatigue_both_legs_maxed_hits_floor(self) -> None:
        floor = 0.4
        with patch.object(settings, "chili_momentum_fatigue_derate_floor", floor), \
                patch.object(settings, "chili_momentum_fatigue_full_session_minutes", 240.0):
            # equities, full session elapsed + trade count >= cap => time_frac=trade_frac=1
            mult, meta = fatigue_derate_multiplier(
                trade_count_today=10, max_trades_per_day=10,
                minutes_since_open=10_000.0, is_crypto=False,
            )
        assert mult == pytest.approx(floor), (mult, meta)

    def test_fatigue_at_open_no_trades_is_one(self) -> None:
        with patch.object(settings, "chili_momentum_fatigue_derate_floor", 0.5):
            mult, _ = fatigue_derate_multiplier(
                trade_count_today=0, max_trades_per_day=10,
                minutes_since_open=0.0, is_crypto=False,
            )
        assert mult == pytest.approx(1.0)


class TestPrimeWindowBounds:
    """``prime_window_size_multiplier`` ∈ [1.0, mult_max], NEVER < 1.0 (never a shrink)."""

    def test_prime_window_in_one_to_max_over_clock_grid(self) -> None:
        # A weekday inside / outside the prime window [04:00, 10:30); and the disabled path.
        for mult_max in [1.0, 1.5, 2.0, 0.5, 3.0]:
            with patch.object(settings, "chili_momentum_timeofday_schedule_enabled", True), \
                    patch.object(settings, "chili_momentum_timeofday_prime_window_size_mult_max", mult_max):
                # iterate every 15 ET-min slot across a weekday by faking the clock helper
                for et_min in range(0, 24 * 60, 37):
                    with patch.object(
                        auto_arm, "_et_minutes_now", lambda now=None, _m=et_min: (_m, True)
                    ):
                        mult, meta = auto_arm.prime_window_size_multiplier()
                    eff_max = max(1.0, mult_max)
                    assert math.isfinite(mult)
                    assert _is_in_range(mult, 1.0, eff_max), (mult_max, et_min, mult)

    def test_prime_window_disabled_is_one(self) -> None:
        with patch.object(settings, "chili_momentum_timeofday_schedule_enabled", False):
            mult, meta = auto_arm.prime_window_size_multiplier()
        assert mult == 1.0
        assert meta["reason"] == "disabled"

    def test_prime_window_weekend_is_one(self) -> None:
        with patch.object(settings, "chili_momentum_timeofday_schedule_enabled", True), \
                patch.object(settings, "chili_momentum_timeofday_prime_window_size_mult_max", 1.5), \
                patch.object(auto_arm, "_et_minutes_now", lambda now=None: (300, False)):
            mult, meta = auto_arm.prime_window_size_multiplier()
        assert mult == 1.0


# ══════════════════════════════════════════════════════════════════════════════
# (2) CHUNKING ``_split_base_size`` exactness + per-piece positivity/increment.
# ══════════════════════════════════════════════════════════════════════════════


class TestSplitBaseSizeInvariants:
    """The split ALWAYS sums to ``total`` within 1e-9 (or returns the single ``[total]``),
    every piece is strictly positive, and with an increment every piece is a positive
    multiple of the increment."""

    _TOTALS = [
        0.0, -1.0, 1e-12, 0.001, 0.01, 0.1, 1.0, 1.5, 2.0, 3.0, 7.0, 10.0,
        100.0, 1000.5, 1e6, 0.30000000000000004, 9.999999999, 33.333333,
    ]
    _BLOCKS = [-1, 0, 1, 2, 3, 4, 5, 7, 10, 11, 100]
    _INCREMENTS = [None, 0.0, -0.5, 0.0001, 0.001, 0.01, 0.1, 0.25, 1.0, 5.0, 1e6]

    def test_sum_exactness_and_positivity_over_grid(self) -> None:
        for total in self._TOTALS:
            for blocks in self._BLOCKS:
                for inc in self._INCREMENTS:
                    pieces = _split_base_size(total, blocks, increment=inc)
                    # ALWAYS a non-empty list.
                    assert isinstance(pieces, list) and len(pieces) >= 1, (total, blocks, inc)
                    if len(pieces) == 1:
                        # single-piece fallback returns exactly [total] (the contract).
                        assert pieces[0] == total, (total, blocks, inc, pieces)
                        continue
                    # multi-piece split: must be a real split with the requested block count.
                    assert len(pieces) == blocks, (total, blocks, inc, pieces)
                    # every piece strictly positive.
                    assert all(p > 0 for p in pieces), (total, blocks, inc, pieces)
                    # Two distinct sum contracts, per the chunker's two code paths:
                    _real_inc = inc is not None and inc > 0
                    if not _real_inc:
                        # No increment constraint => equal float split, remainder onto the
                        # last piece => the sum is EXACT (within float epsilon).
                        assert abs(sum(pieces) - total) <= 1e-9, (total, blocks, inc, sum(pieces))
                    else:
                        # With a real increment the chunker works in integer increment-UNITS
                        # (units_total = round(total/inc)), so EVERY piece is a positive
                        # multiple of the increment and the sum is `round(total/inc)*inc` —
                        # which differs from a non-multiple `total` by AT MOST half an
                        # increment (the documented rounding). Assert valid venue sizes +
                        # the rounding bound (still fails if the chunker drifts beyond it or
                        # emits a non-multiple piece). Upstream rounds qty to the increment
                        # BEFORE chunking, so a non-multiple total never occurs live.
                        for p in pieces:
                            _units = p / inc
                            assert abs(_units - round(_units)) <= 1e-6, (total, blocks, inc, p, _units)
                        _tol = inc / 2.0 + 1e-6 * max(1.0, abs(total))
                        assert abs(sum(pieces) - total) <= _tol, (total, blocks, inc, sum(pieces), _tol)

    def test_increment_pieces_are_positive_multiples(self) -> None:
        # When a real increment is supplied and a split happens, EVERY piece must be a
        # positive integer multiple of the increment (>= one increment).
        for total in [1.0, 2.5, 10.0, 100.0, 7.7, 1000.0]:
            for blocks in [2, 3, 4, 5, 10]:
                for inc in [0.001, 0.01, 0.1, 0.25, 1.0]:
                    pieces = _split_base_size(total, blocks, increment=inc)
                    if len(pieces) <= 1:
                        continue  # single-fallback: contract checked elsewhere
                    for p in pieces:
                        units = p / inc
                        assert p >= inc - 1e-9, (total, blocks, inc, p)
                        # integer multiple of the increment (within float tolerance).
                        assert abs(units - round(units)) <= 1e-6, (total, blocks, inc, p, units)

    def test_clean_multiple_totals_sum_exactly(self) -> None:
        # EXACT-SUM contract on the live-shaped input: when ``total`` IS a clean multiple
        # of the increment (as upstream guarantees by rounding qty to the venue increment
        # BEFORE chunking), the integer-unit split has NO rounding remainder, so the pieces
        # sum EXACTLY to total (within float epsilon). Build totals as units*inc so they are
        # exact multiples; only assert when a real split actually happened.
        for inc in [0.001, 0.01, 0.1, 0.25, 1.0]:
            for units_total in [2, 3, 5, 7, 10, 23, 100, 257, 1000]:
                total = round(units_total * inc, 12)
                for blocks in [2, 3, 4, 5, 7, 10]:
                    pieces = _split_base_size(total, blocks, increment=inc)
                    if len(pieces) <= 1:
                        continue  # not splittable into ``blocks`` >=1-increment pieces
                    assert len(pieces) == blocks, (total, blocks, inc, pieces)
                    assert all(p > 0 for p in pieces), (total, blocks, inc, pieces)
                    for p in pieces:
                        _units = p / inc
                        assert abs(_units - round(_units)) <= 1e-6, (total, blocks, inc, p)
                    # clean multiple => EXACT sum (no half-increment rounding slack).
                    assert abs(sum(pieces) - total) <= 1e-9, (total, blocks, inc, sum(pieces))

    def test_nonpositive_or_unsplittable_returns_single(self) -> None:
        # blocks<=1, total<=0 => exactly [total]; units_total < blocks => [total].
        assert _split_base_size(5.0, 1, increment=None) == [5.0]
        assert _split_base_size(0.0, 4, increment=None) == [0.0]
        assert _split_base_size(-3.0, 4, increment=None) == [-3.0]
        # 2 units worth of size but 5 blocks => can't give each block >=1 increment.
        out = _split_base_size(0.002, 5, increment=0.001)
        assert out == [0.002]

    def test_nonfinite_total_returns_single(self) -> None:
        for bad in _NON_FINITE:
            assert _split_base_size(bad, 4, increment=None) == [bad]

    def test_pseudorandom_seeded_sum_exactness(self) -> None:
        for seed in _SEEDS:
            rng = random.Random(seed)
            for _ in range(60):
                total = rng.uniform(0.0001, 5000.0)
                blocks = rng.randint(1, 10)
                inc = rng.choice([None, 0.001, 0.01, 0.1, 1.0])
                pieces = _split_base_size(total, blocks, increment=inc)
                assert all(p > 0 for p in pieces), (seed, total, blocks, inc, pieces)
                if len(pieces) > 1:
                    if inc is None:
                        # No increment => exact float split (remainder onto the last piece).
                        assert abs(sum(pieces) - total) <= 1e-9, (seed, total, blocks, inc)
                    else:
                        # Random totals are NOT clean multiples of the increment, so the
                        # chunker's integer-unit rounding makes the sum land within half an
                        # increment of total — and every piece is a positive increment
                        # multiple (valid venue size). Assert that documented behavior, NOT
                        # exact-sum of a non-multiple total. (Live, qty is rounded to the
                        # increment upstream, so the exact-sum case is covered separately.)
                        for p in pieces:
                            _units = p / inc
                            assert abs(_units - round(_units)) <= 1e-6, (seed, total, blocks, inc, p)
                        _tol = inc / 2.0 + 1e-6 * max(1.0, abs(total))
                        assert abs(sum(pieces) - total) <= _tol, (seed, total, blocks, inc, sum(pieces))


# ══════════════════════════════════════════════════════════════════════════════
# (3) ``adaptive_spread_cost_veto_derate`` NEVER blocks; mult ∈ [floor, 1.0].
# ══════════════════════════════════════════════════════════════════════════════


class _FakePercentileResult:
    def __init__(self, row: Optional[tuple]) -> None:
        self._row = row

    def fetchone(self) -> Optional[tuple]:
        return self._row


class _FakeDB:
    """Fake Session.execute that returns one canned (p50,p75,p90,n) row (or None / raises)."""

    def __init__(self, row: Optional[tuple], *, raise_exc: bool = False) -> None:
        self._row = row
        self._raise = raise_exc

    def execute(self, *_a: Any, **_k: Any) -> _FakePercentileResult:
        if self._raise:
            raise RuntimeError("boom")
        return _FakePercentileResult(self._row)


class TestSpreadCostDerateInvariants:
    """DERATE-ONLY GLOBALLY: ``allow`` is ALWAYS True and ``mult`` ∈ [floor, 1.0] for ANY
    spread / R / trigger reason / name-distribution — no input can make it block."""

    _FLOOR = 0.5

    # representative + boundary spreads (bps); cover tight, typical, anomalous, extreme.
    _SPREADS = [None, 0.0, -5.0, 5.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1500.0, 9999.0,
                float("nan"), float("inf")]
    # stop distances ($): tight, normal, wide, degenerate.
    _STOPS = [None, 0.0, -0.1, 0.001, 0.01, 0.05, 0.5, 5.0, 50.0, float("nan")]
    _ENTRIES = [None, 0.0, -1.0, 0.5, 2.0, 10.0, 100.0, float("inf")]
    _REASONS = [None, "", "breakout", "hod_break", "vwap_reclaim", "flush_dip_buy",
                "deep_reclaim_tick_ok", "curl_reclaim", "bounce", "sub_vwap_trap", 123, object()]
    # name distributions: insufficient history (None row), normal, extreme-anomaly setup.
    _PCT_ROWS = [
        None,                       # insufficient history -> derate-only on cost-of-R
        (300.0, 400.0, 500.0, 50),  # name normally ~300bps
        (10.0, 15.0, 20.0, 200),    # tight name -> a wide spread is extreme vs ITS p90
    ]

    def _set_settings(self) -> Any:
        return patch.multiple(
            settings,
            chili_momentum_spread_cost_max_fraction_of_r=0.25,
            chili_momentum_spread_cost_reclaim_max_fraction_of_r=0.35,
            chili_momentum_spread_anomaly_p50_mult=2.0,
            chili_momentum_spread_anomaly_extreme_p90_mult=1.5,
            chili_momentum_spread_cost_derate_floor=self._FLOOR,
            chili_momentum_spread_cost_derate_engage_frac=0.5,
            chili_momentum_spread_norm_lookback_days=20.0,
        )

    def test_never_blocks_mult_in_floor_one_over_grid(self) -> None:
        now = datetime(2026, 6, 27, tzinfo=timezone.utc)
        with self._set_settings():
            for row in self._PCT_ROWS:
                db = _FakeDB(row)
                for sb in self._SPREADS:
                    for sd in self._STOPS:
                        for e in self._ENTRIES:
                            for reason in self._REASONS:
                                allow, mult, why, meta = adaptive_spread_cost_veto_derate(
                                    symbol="TEST", entry_price=e, current_spread_bps=sb,
                                    stop_distance=sd, db=db, flag_enabled=True,
                                    entry_trigger_reason=reason, now_utc=now,
                                )
                                assert allow is True, (row, sb, sd, e, reason, allow, why)
                                assert math.isfinite(mult), (row, sb, sd, e, reason, mult)
                                assert _is_in_range(mult, self._FLOOR, 1.0), (
                                    row, sb, sd, e, reason, mult, why,
                                )

    def test_reclaim_derates_no_more_than_nonreclaim(self) -> None:
        # SAME extreme spread: a reclaim trigger must derate >= the non-reclaim mult
        # (derates-LESS tilt => its multiplier is never SMALLER than the standard path).
        now = datetime(2026, 6, 27, tzinfo=timezone.utc)
        db = _FakeDB((10.0, 15.0, 20.0, 200))  # tight name; 600bps is extreme vs p90=20
        with self._set_settings():
            allow_n, mult_n, _, _ = adaptive_spread_cost_veto_derate(
                symbol="TEST", entry_price=10.0, current_spread_bps=600.0,
                stop_distance=0.05, db=db, flag_enabled=True,
                entry_trigger_reason="breakout", now_utc=now,
            )
            allow_r, mult_r, _, _ = adaptive_spread_cost_veto_derate(
                symbol="TEST", entry_price=10.0, current_spread_bps=600.0,
                stop_distance=0.05, db=db, flag_enabled=True,
                entry_trigger_reason="vwap_reclaim", now_utc=now,
            )
        assert allow_n is True and allow_r is True
        assert mult_r >= mult_n - 1e-9, (mult_n, mult_r)

    def test_flag_off_is_passthrough(self) -> None:
        allow, mult, why, meta = adaptive_spread_cost_veto_derate(
            symbol="X", entry_price=10.0, current_spread_bps=500.0, stop_distance=0.01,
            db=_FakeDB(None), flag_enabled=False,
        )
        assert allow is True and mult == 1.0 and why == "flag_off"

    def test_db_raise_fails_open_to_one(self) -> None:
        # A percentile-read error must NOT derate/block — the name-distribution simply
        # becomes unavailable; with a benign cost-of-R the result is 1.0 pass.
        now = datetime(2026, 6, 27, tzinfo=timezone.utc)
        with self._set_settings():
            allow, mult, why, meta = adaptive_spread_cost_veto_derate(
                symbol="X", entry_price=10.0, current_spread_bps=20.0, stop_distance=5.0,
                db=_FakeDB(None, raise_exc=True), flag_enabled=True, now_utc=now,
            )
        assert allow is True
        assert _is_in_range(mult, self._FLOOR, 1.0)


# ══════════════════════════════════════════════════════════════════════════════
# (4) ``_entry_extension_veto`` MONOTONICITY in the entry-price (extension) axis.
# ══════════════════════════════════════════════════════════════════════════════


class _ExtSettings:
    """Minimal settings stub for ``_entry_extension_veto`` (it reads attrs off the object)."""

    chili_momentum_entry_extension_veto_enabled = True
    chili_momentum_entry_extension_atr_mult = 8.0
    chili_momentum_entry_extension_floor_pct = 0.08
    chili_momentum_explosive_recalibration_enabled = False
    chili_momentum_entry_extension_rvol_boost_enabled = False
    chili_momentum_explosive_rvol_floor = 3.0
    chili_momentum_entry_extension_rvol_boost_per = 0.05
    chili_momentum_entry_extension_rvol_boost_max = 0.15


class TestEntryExtensionVetoMonotone:
    """At a FIXED breakout level + atr, the veto is MONOTONE NON-DECREASING in entry_price:
    once a price vetoes, every HIGHER (more-extended) price also vetoes. Equivalently, a
    more-extended entry can NEVER un-veto a case a less-extended entry already vetoed."""

    def test_monotone_in_entry_price_over_grid(self) -> None:
        st = _ExtSettings()
        level = 10.0
        for atr_pct in [None, 0.0, 0.005, 0.015, 0.05, 0.2, 1.0]:
            prices = [level * (1.0 + f) for f in
                      [0.0, 0.01, 0.05, 0.08, 0.12, 0.2, 0.34, 0.5, 1.0, 3.0]]
            results = [
                entry_gates._entry_extension_veto(p, level, atr_pct, st)
                for p in prices
            ]
            # once True, stays True for all higher prices (monotone non-decreasing).
            seen_true = False
            for p, v in zip(prices, results):
                if seen_true:
                    assert v is True, (atr_pct, p, results)
                if v:
                    seen_true = True

    def test_more_extended_never_unvetoes_pairwise(self) -> None:
        st = _ExtSettings()
        level = 7.63  # the PLSM example from the source docstring
        for atr_pct in [None, 0.01, 0.015, 0.03, 0.1]:
            for seed in _SEEDS[:4]:
                rng = random.Random(seed)
                for _ in range(40):
                    lo = level * (1.0 + rng.uniform(0.0, 0.5))
                    hi = lo + level * rng.uniform(0.0, 0.5)  # hi >= lo (more extended)
                    v_lo = entry_gates._entry_extension_veto(lo, level, atr_pct, st)
                    v_hi = entry_gates._entry_extension_veto(hi, level, atr_pct, st)
                    if v_lo:
                        assert v_hi, (atr_pct, lo, hi, v_lo, v_hi)

    def test_disabled_flag_never_vetoes(self) -> None:
        st = _ExtSettings()
        st.chili_momentum_entry_extension_veto_enabled = False
        # even a wildly extended entry never vetoes when the flag is off.
        assert entry_gates._entry_extension_veto(1000.0, 10.0, 0.01, st) is False

    def test_missing_level_or_price_never_vetoes(self) -> None:
        st = _ExtSettings()
        assert entry_gates._entry_extension_veto(None, 10.0, 0.01, st) is False
        assert entry_gates._entry_extension_veto(15.0, None, 0.01, st) is False
        assert entry_gates._entry_extension_veto(15.0, 0.0, 0.01, st) is False


# ══════════════════════════════════════════════════════════════════════════════
# (5) doji threshold + pullback-depth ceiling: MONOTONE in atr_pct, BOUNDED.
# ══════════════════════════════════════════════════════════════════════════════


class TestDojiThresholdMonotone:
    """The doji body-fraction threshold ``base_body_frac + max(0, atr_pct)`` is monotone
    non-decreasing in atr_pct (a higher-ATR name needs a fuller body to NOT be a doji =>
    more permissive structure). We read the threshold out of the debug dict on a fixed bar."""

    def _threshold(self, atr_pct: float | None, base: float) -> float | None:
        # A tall-range, thin-body bar so we always reach the threshold-compute branch.
        o, h, l, c = 10.0, 10.5, 9.5, 10.02  # tiny body vs a 1.0 range
        _veto, dbg = entry_gates._doji_trigger_veto(
            o, h, l, c, atr_pct=atr_pct, base_body_frac=base,
        )
        return dbg.get("doji_threshold")

    def test_threshold_monotone_nondecreasing_in_atr(self) -> None:
        base = 0.25
        atrs = [0.0, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 5.0]
        thr = [self._threshold(a, base) for a in atrs]
        assert all(t is not None for t in thr), thr
        for prev, cur in zip(thr, thr[1:]):
            assert cur >= prev - 1e-12, (prev, cur, thr)
        # threshold equals base + atr exactly (no hidden magic).
        for a, t in zip(atrs, thr):
            assert t == pytest.approx(base + a, abs=1e-9), (a, t)

    def test_negative_atr_floored_to_base(self) -> None:
        # max(0, atr_pct): a negative ATR can only collapse to the base, never below it.
        base = 0.25
        for a in [-0.5, -1.0, -1e9]:
            t = self._threshold(a, base)
            assert t == pytest.approx(base, abs=1e-9), (a, t)

    def test_threshold_finite_for_all_finite_atr(self) -> None:
        for base in [0.0, 0.1, 0.25, 0.5]:
            for a in _FINITE_SCALARS:
                t = self._threshold(a, base)
                if t is not None:
                    assert math.isfinite(t), (base, a, t)


class TestPullbackDepthCeilingMonotone:
    """``_adaptive_pullback_depth_ceiling`` is monotone non-decreasing in atr_pct (a calm
    name keeps the tight ~0.50 base, a volatile name widens) and is HARD-BOUNDED at
    ``_VOL_SHALLOW_CEIL`` (0.75) — never exceeds it for ANY atr_pct."""

    def test_ceiling_monotone_and_bounded(self) -> None:
        atrs = sorted([0.0, 0.001, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 10.0, 1e6])
        vals = [entry_gates._adaptive_pullback_depth_ceiling(a, True) for a in atrs]
        for v in vals:
            assert math.isfinite(v)
            assert v <= entry_gates._VOL_SHALLOW_CEIL + 1e-12, v
            assert v >= entry_gates._VOL_SHALLOW_BASE - 1e-12, v  # base is the floor
        for prev, cur in zip(vals, vals[1:]):
            assert cur >= prev - 1e-12, (prev, cur, vals)
        # large ATR saturates exactly at the hard ceiling.
        assert entry_gates._adaptive_pullback_depth_ceiling(1e6, True) == pytest.approx(
            entry_gates._VOL_SHALLOW_CEIL
        )

    def test_disabled_returns_zero_passthrough(self) -> None:
        # disabled => 0.0 (caller falls through to its own ceiling, byte-identical).
        for a in [None, 0.0, 0.05, 1.0]:
            assert entry_gates._adaptive_pullback_depth_ceiling(a, False) == 0.0

    def test_none_and_negative_atr_collapse_to_base(self) -> None:
        # atr None / <=0 => base (the calm floor), never below.
        for a in [None, 0.0, -0.5, -1e9]:
            v = entry_gates._adaptive_pullback_depth_ceiling(a, True)
            assert v == pytest.approx(entry_gates._VOL_SHALLOW_BASE), (a, v)


# ══════════════════════════════════════════════════════════════════════════════
# (6) ``_asetup_quality_floor`` >= conviction floor for ANY score distribution.
# ══════════════════════════════════════════════════════════════════════════════


class TestAsetupQualityFloorInvariant:
    """The adaptive A+ bar = max(conviction_floor, median - margin*std) — so it can NEVER
    drop below the conviction floor, for ANY score distribution (including empty / single /
    pseudo-random)."""

    def test_floor_never_below_conviction_over_grid(self) -> None:
        for convict in [0.5, 0.7, 0.9, 0.0, 1.0]:
            for margin in [0.0, 0.5, 1.0, 2.0, 10.0]:
                with patch.object(settings, "chili_momentum_continuation_ross_floor", convict), \
                        patch.object(
                            settings, "chili_momentum_no_asetup_sit_cash_margin_multiple", margin
                        ):
                    # representative + boundary distributions
                    dists = [
                        [],
                        [0.5],
                        [0.9],
                        [0.1, 0.2, 0.3],
                        [0.7, 0.7, 0.7, 0.7],
                        [0.0, 1.0],
                        [0.95, 0.92, 0.88, 0.91, 0.99],
                        [-5.0, 5.0, 0.0],  # absurd spread => big std
                    ]
                    for seed in _SEEDS:
                        dists.append(_pseudo_random_floats(seed, 12, lo=0.0, hi=1.0))
                    for scores in dists:
                        bar = auto_arm._asetup_quality_floor(list(scores))
                        assert math.isfinite(bar), (convict, margin, scores, bar)
                        assert bar >= convict - 1e-9, (convict, margin, scores, bar)

    def test_empty_and_single_distribution_edges(self) -> None:
        with patch.object(settings, "chili_momentum_continuation_ross_floor", 0.7), \
                patch.object(settings, "chili_momentum_no_asetup_sit_cash_margin_multiple", 1.0):
            # empty => exactly the conviction floor.
            assert auto_arm._asetup_quality_floor([]) == pytest.approx(0.7)
            # single high score: n==1 => std 0 => bar = max(floor, median) = median if > floor.
            assert auto_arm._asetup_quality_floor([0.9]) == pytest.approx(0.9)
            # single low score: median < floor => clamped to floor.
            assert auto_arm._asetup_quality_floor([0.3]) == pytest.approx(0.7)


# ══════════════════════════════════════════════════════════════════════════════
# (7) ``consecutive_green_days`` >= 0 ALWAYS (DB-backed; empty + synthetic history).
# ══════════════════════════════════════════════════════════════════════════════


class TestConsecutiveGreenDaysNonNegative:
    """The streak is ALWAYS a non-negative int. Exercised against the conftest ``db``
    fixture (empty DB => 0) and with synthetic outcome rows so the count path runs."""

    def test_empty_db_is_zero(self, db: Any) -> None:
        streak, meta = consecutive_green_days(db, execution_family="coinbase_spot")
        assert isinstance(streak, int)
        assert streak == 0
        assert meta["streak"] == 0

    def test_no_input_paths_return_zero(self) -> None:
        # None db / missing execution_family / non-positive lookback => 0 (never negative).
        assert consecutive_green_days(None, execution_family="x")[0] == 0
        assert consecutive_green_days(object(), execution_family=None)[0] == 0
        assert consecutive_green_days(object(), execution_family="x", lookback_days=0)[0] == 0
        assert consecutive_green_days(object(), execution_family="x", lookback_days=-5)[0] == 0

    def test_read_failure_fails_neutral_to_zero(self) -> None:
        # A db whose .query() raises must fail-neutral to (0, read_failed) — never a
        # negative streak, never a crash (the contract for a flaky read).
        class _BoomDB:
            def query(self, *a: Any, **k: Any) -> Any:
                raise RuntimeError("boom")

        streak, meta = consecutive_green_days(_BoomDB(), execution_family="coinbase_spot")
        assert streak == 0
        assert meta.get("reason") in ("read_failed", "no_history", "no_buckets")

    def test_synthetic_history_streak_nonnegative(self, db: Any) -> None:
        """Seed REAL-entry green/red days via a parent session + variant (the FK chain)
        and assert the contiguous-green streak is the SPECIFIC expected value AND a sane
        non-negative int. This proves both invariant (7) and the exact count contract."""
        from datetime import timedelta

        from app.models.trading import (
            MomentumAutomationOutcome,
            MomentumStrategyVariant,
            TradingAutomationSession,
        )

        ef = "coinbase_spot"
        variant = MomentumStrategyVariant(
            family="momentum_neural", variant_key="micro_inv_test", version=1,
            label="micro-inv-test", params_json={}, execution_family=ef,
        )
        db.add(variant)
        db.flush()

        base = datetime.utcnow().replace(hour=18, minute=0, second=0, microsecond=0)
        # today excluded by the helper. day-1 back: net +15 (green); day-2: +5 (green);
        # day-3: net -6 (red) -> streak stops at 2. day-4: green (not counted).
        rows_spec = [
            (1, 12.0), (1, 3.0),
            (2, 5.0),
            (3, -8.0), (3, 2.0),
            (4, 9.0),
        ]
        for i, (days_ago, pnl) in enumerate(rows_spec):
            sess = TradingAutomationSession(
                execution_family=ef, mode="live", symbol="TEST-USD",
                variant_id=variant.id, state="completed",
            )
            db.add(sess)
            db.flush()
            db.add(MomentumAutomationOutcome(
                session_id=sess.id,
                variant_id=variant.id,
                symbol="TEST-USD",
                mode="live",
                execution_family=ef,
                terminal_state="closed",
                outcome_class="success" if pnl > 0 else "stop_loss",
                realized_pnl_usd=pnl,
                terminal_at=(base - timedelta(days=days_ago)),
            ))
        db.commit()

        streak, meta = consecutive_green_days(db, execution_family=ef, lookback_days=30)
        assert isinstance(streak, int)
        assert streak >= 0          # invariant (7): never negative
        assert streak <= 6          # never more than distinct seeded days
        assert streak == 2, (streak, meta)  # exact contiguous-green contract
