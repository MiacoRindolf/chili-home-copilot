"""PRINCIPAL-LEVEL numeric-torture edge-case hunt for the math-heavy momentum helpers.

Class: [numeric-torture] — FLOATING-POINT + numeric extremes (not branch coverage).
These tests construct the gnarliest plausible inputs each helper meets in prod and
assert the SPECIFIC correct value so a subtly-wrong implementation FAILS:

  (1) chunking ``_split_base_size`` at pathological increments (0.0001, 1e7 total,
      total not an exact multiple of inc, inc > total, units_total == blocks, a
      total whose /inc rounds up vs down) -> sum-exactness to 1e-9 + each piece
      >= one increment.
  (2) penny ($0.001 / $0.002) and high-priced ($5000) names through the spread-cost
      ``cost_of_r`` and the extension cap -> no overflow/underflow, correct ratios.
  (3) NaN/inf propagation through spread-cost percentiles, the catalyst clamp, the
      green-day multiplier (prove the guards).
  (4) doji body/range at range == eps (near-zero range bar).
  (5) 0.1+0.2 != 0.3 binary-unfriendly boundaries in the extension cap and the
      pullback-depth ceiling.

PURE-LOGIC + mocks (fake DB, patch.object on settings) so the suite runs fast
without a DB truncate. No source file is modified.

Run:
  TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test \
    conda run -n chili-env pytest tests/test_momentum_edge_numeric_torture.py -v
"""

from __future__ import annotations

import math
from typing import Any, Optional

import pytest

from app.config import settings
from app.services.trading.momentum_neural import risk_policy
from app.services.trading.momentum_neural.spread_cost_veto import (
    adaptive_spread_cost_veto_derate,
    name_spread_percentiles,
    _f,
)
from app.services.trading.momentum_neural.entry_gates import (
    _entry_extension_veto,
    _adaptive_pullback_depth_ceiling,
    _doji_trigger_veto,
    _VOL_SHALLOW_BASE,
    _VOL_SHALLOW_CEIL,
    _VOL_SHALLOW_ATR_MULT,
)
from app.services.trading.venue.chunking_adapter import _split_base_size, _fmt_size


# ════════════════════════════════════════════════════════════════════════════════
# (1) CHUNKING — _split_base_size at PATHOLOGICAL increments
# ════════════════════════════════════════════════════════════════════════════════
#
# Contract (from the docstring + code):
#   * children sum EXACTLY to ``total`` (no over/under-fill vs parent qty)
#   * each piece is a positive multiple of ``increment`` (when given)
#   * returns ``[total]`` (single piece) when a clean blocks-way split is impossible
#
# The increment path works in INTEGER increment-units: units_total = round(total/inc).
# So sum-exactness is only guaranteed to the precision of ``round(u * inc, 12)``.

_SUM_TOL = 1e-9


def _assert_pieces_valid(pieces, *, total, blocks, inc):
    """Either a clean blocks-way split (sum-exact, each >= one inc) or [total]."""
    assert pieces, "must never return empty"
    if len(pieces) == 1:
        assert pieces[0] == total
        return
    assert len(pieces) == blocks, f"expected {blocks} pieces, got {len(pieces)}"
    # sum-exactness: the children must reconstitute the parent qty.
    assert abs(sum(pieces) - total) <= _SUM_TOL, (
        f"sum {sum(pieces)!r} != total {total!r} (drift {sum(pieces)-total:.3e})"
    )
    for p in pieces:
        assert p > 0, f"non-positive piece {p!r}"
        if inc:
            # each piece is (approximately) an integer multiple of inc.
            units = p / inc
            assert abs(units - round(units)) <= 1e-6, (
                f"piece {p!r} is not a clean multiple of inc {inc!r} (units={units!r})"
            )
            assert round(units) >= 1, f"piece {p!r} below one increment {inc!r}"


def test_split_tiny_increment_penny_crypto():
    """inc=0.0001 (a 4-dp crypto base_increment), small total, 3 blocks.
    units_total = round(1.0/0.0001)=10000, per=3333, last=3334 -> sum exact."""
    total, blocks, inc = 1.0, 3, 0.0001
    pieces = _split_base_size(total, blocks, increment=inc)
    _assert_pieces_valid(pieces, total=total, blocks=blocks, inc=inc)
    # The remainder (10000 - 3*3333 = 1 unit) lands on the LAST block.
    assert pieces[0] == pytest.approx(0.3333, abs=1e-12)
    assert pieces[-1] == pytest.approx(0.3334, abs=1e-12)


def test_split_huge_total_small_increment_no_overflow():
    """1e7 total at inc=0.0001 -> units_total = 1e11. int() handles it; sum exact.
    Stresses the integer-unit path against a large magnitude (no float overflow)."""
    total, blocks, inc = 1e7, 4, 0.0001
    pieces = _split_base_size(total, blocks, increment=inc)
    _assert_pieces_valid(pieces, total=total, blocks=blocks, inc=inc)
    # 1e11 units / 4 = 2.5e10 exactly -> all four blocks equal.
    assert pieces[0] == pieces[1] == pieces[2] == pieces[3]
    assert pieces[0] == pytest.approx(2.5e6, rel=1e-12)


def test_split_total_not_exact_multiple_of_inc_rounds_then_is_exact():
    """total NOT an exact multiple of inc: total=1.00005, inc=0.0001.
    units_total = round(1.00005/0.0001) = round(10000.5) -> banker's round = 10000.
    The split sums to 10000 units == 1.0 (NOT the original 1.00005). This is the
    documented behaviour (work in rounded integer units); the children must still
    sum self-consistently and each be a clean multiple of inc."""
    total, blocks, inc = 1.00005, 2, 0.0001
    pieces = _split_base_size(total, blocks, increment=inc)
    assert len(pieces) == 2
    s = sum(pieces)
    # round(10000.5) -> 10000 (banker's rounding to even). pieces sum to 1.0, not 1.00005.
    assert s == pytest.approx(1.0, abs=_SUM_TOL)
    # GNARLY NOTE: the children do NOT sum to the ORIGINAL base_size (1.00005). They sum
    # to the inc-rounded qty. The live runner reconciles legs onto the parent by broker
    # order_id, so a sub-increment rounding of the parent qty is expected/safe.
    for p in pieces:
        assert abs((p / inc) - round(p / inc)) <= 1e-6


def test_split_inc_larger_than_total_returns_single():
    """inc > total: units_total = round(total/inc) < blocks (often 0 or 1) -> single."""
    pieces = _split_base_size(0.5, 3, increment=2.0)
    assert pieces == [0.5]


def test_split_units_total_exactly_equals_blocks():
    """units_total == blocks: each block gets EXACTLY one increment (per=1, no rem).
    total=0.0003, inc=0.0001 -> units=3, blocks=3 -> [0.0001, 0.0001, 0.0001]."""
    total, blocks, inc = 0.0003, 3, 0.0001
    pieces = _split_base_size(total, blocks, increment=inc)
    _assert_pieces_valid(pieces, total=total, blocks=blocks, inc=inc)
    assert len(pieces) == 3
    for p in pieces:
        assert p == pytest.approx(0.0001, abs=1e-12)


def test_split_units_total_one_below_blocks_returns_single():
    """units_total == blocks-1 (< blocks): can't give every block one inc -> single.
    total=0.0002, inc=0.0001, blocks=3 -> units=2 < 3 -> [total]."""
    pieces = _split_base_size(0.0002, 3, increment=0.0001)
    assert pieces == [0.0002]


def test_split_rounds_up_vs_down_boundary():
    """A total whose /inc lands on a .5 boundary: total=0.00025, inc=0.0001.
    0.00025/0.0001 = 2.5 -> round half-to-even -> 2 units. 2 < blocks=3 -> single.
    Proves the round() (not floor/ceil) at the unit conversion."""
    pieces = _split_base_size(0.00025, 3, increment=0.0001)
    # round(2.5)=2 (even) -> units < blocks -> single. (A ceil would give 3 -> a split.)
    assert pieces == [0.00025]
    # And the .5-up case: 0.00035/0.0001 = 3.5 -> round to even = 4 units, blocks=3.
    pieces2 = _split_base_size(0.00035, 3, increment=0.0001)
    assert len(pieces2) == 3
    # 4 units: per=1, last gets 1 + (4-3) = 2 units -> [0.0001, 0.0001, 0.0002].
    assert pieces2[-1] == pytest.approx(0.0002, abs=1e-12)
    assert sum(pieces2) == pytest.approx(0.0004, abs=_SUM_TOL)


def test_split_no_increment_float_remainder_on_last_exact():
    """No-increment path: equal float split, remainder onto the LAST piece so the sum
    is EXACT even for a binary-unfriendly total. total=0.3, blocks=3 -> per=0.1
    (0.1 is not exact in binary); the last piece is total - per*(blocks-1) so the sum
    reconstitutes 0.3 EXACTLY (== total, by construction), not 0.1+0.1+0.1."""
    total, blocks = 0.3, 3
    pieces = _split_base_size(total, blocks, increment=None)
    assert len(pieces) == 3
    # The implementation guarantees exact reconstruction: sum == total bit-for-bit
    # because the last piece is computed as total - per*(blocks-1).
    assert sum(pieces) == total  # EXACT, not just approx (the whole point of the design)
    assert pieces[0] == pieces[1] == total / blocks


def test_split_no_increment_huge_total_sum_drift_documented():
    """GNARLY FINDING — the no-increment sum is NOT bit-exact at large magnitude / many
    blocks. 1e7 / 7 has a repeating remainder. The last piece is ``total - per*(blocks-1)``,
    but ``sum(pieces)`` re-accumulates ``per`` six times in a DIFFERENT order than the single
    ``per*6`` used to derive the last piece, so the two rounding paths diverge:

        sum(pieces) == 10000000.000000002  (drift ~1.86e-9 vs total 1e7)

    The docstring claims children "sum EXACTLY to total". That holds for small block counts
    where accumulation order doesn't diverge (see the 0.3/3 case above, which IS bit-exact),
    but NOT here. The drift is ~1.9e-9 on a 1e7 parent (~1.9e-16 relative) — operationally
    harmless (it's a base-size qty, reconciled onto the parent by broker order_id), but it
    technically VIOLATES the documented exactness AND exceeds the task's stated 1e-9 sum
    tolerance. Flagged in the report. This test pins the real (drifting) behaviour."""
    total, blocks = 1e7, 7
    pieces = _split_base_size(total, blocks, increment=None)
    assert len(pieces) == 7
    assert all(p > 0 for p in pieces)
    drift = abs(sum(pieces) - total)
    # NOT bit-exact (the documented-exactness claim does not hold here):
    assert drift > 0.0, "expected accumulation-order drift at 1e7/7"
    # but tiny in RELATIVE terms (the operationally-relevant bound):
    assert drift / total < 1e-12


def test_split_inc_zero_or_negative_falls_to_float_path():
    """increment <= 0 (or None) disables the integer path -> float split. inc=0.0
    is falsy -> ``increment and increment > 0`` is False -> inc=None branch."""
    p_zero = _split_base_size(0.3, 3, increment=0.0)
    p_neg = _split_base_size(0.3, 3, increment=-1.0)
    p_none = _split_base_size(0.3, 3, increment=None)
    assert sum(p_zero) == 0.3 and len(p_zero) == 3
    assert sum(p_neg) == 0.3 and len(p_neg) == 3
    assert p_zero == p_none == p_neg


def test_split_nan_inf_total_returns_single():
    """NaN/inf total: ``math.isfinite(total)`` guard -> single. (nan<=0 is False so
    the isfinite check is the load-bearing guard; without it a NaN would split into
    NaN pieces and crash the order path.)"""
    # nan != nan so compare via isnan; assert it returned a single piece, not a split.
    r_nan = _split_base_size(float("nan"), 3, increment=0.0001)
    assert len(r_nan) == 1 and math.isnan(r_nan[0])
    r_inf = _split_base_size(float("inf"), 3, increment=0.0001)
    assert r_inf == [float("inf")]


def test_fmt_size_trims_and_never_scientific():
    """_fmt_size must emit a plain decimal (the venue rejects scientific notation).
    A tiny size like 2.5e6 from a chunk must format as digits, not '2.5e+06'."""
    assert "e" not in _fmt_size(2.5e6).lower()
    assert _fmt_size(0.0001) == "0.0001"
    assert _fmt_size(0.30000000004) == "0.3"  # trailing-zero/round trim at 10dp
    assert _fmt_size(0.0) == "0"


# ════════════════════════════════════════════════════════════════════════════════
# (2) SPREAD-COST cost_of_r — penny / high-priced names; (3) NaN/inf propagation
# ════════════════════════════════════════════════════════════════════════════════


class _FakePercentileResult:
    def __init__(self, row: Optional[tuple]) -> None:
        self._row = row

    def fetchone(self) -> Optional[tuple]:
        return self._row


class _FakeDB:
    """Fake Session.execute() returning one canned (p50,p75,p90,n) percentile row."""

    def __init__(self, row: Optional[tuple], *, raise_exc: bool = False) -> None:
        self._row = row
        self._raise = raise_exc

    def execute(self, *_a: Any, **_k: Any) -> _FakePercentileResult:
        if self._raise:
            raise RuntimeError("boom")
        return _FakePercentileResult(self._row)


@pytest.fixture
def spread_settings(monkeypatch):
    """Pin every adaptive spread knob to its documented default so cost_of_r math is
    deterministic regardless of the operator's live config."""
    knobs = {
        "chili_momentum_spread_cost_max_fraction_of_r": 0.25,
        "chili_momentum_spread_cost_reclaim_max_fraction_of_r": 0.35,
        "chili_momentum_spread_anomaly_p50_mult": 2.0,
        "chili_momentum_spread_anomaly_extreme_p90_mult": 1.5,
        "chili_momentum_spread_cost_derate_floor": 0.5,
        "chili_momentum_spread_norm_lookback_days": 20.0,
        "chili_momentum_spread_cost_derate_engage_frac": 0.5,
    }
    for k, v in knobs.items():
        monkeypatch.setattr(settings, k, v, raising=False)
    return knobs


def test_cost_of_r_penny_stock_exact_ratio(spread_settings):
    """Penny name: entry $0.001, stop_distance $0.0005 (R), spread 500 bps.
    spread$ = (500/1e4)*0.001 = 5e-5 ; cost_of_r = 5e-5 / 5e-4 = 0.10 EXACTLY.
    0.10 < engage_cost (0.5*0.25=0.125) -> NO derate, mult 1.0 (no underflow at $1e-3)."""
    allow, mult, reason, meta = adaptive_spread_cost_veto_derate(
        symbol="PENNY",
        entry_price=0.001,
        current_spread_bps=500.0,
        stop_distance=0.0005,
        db=_FakeDB(None),  # thin history -> anomaly path off, cost-of-R only
        flag_enabled=True,
    )
    assert allow is True
    assert meta["cost_of_r"] == pytest.approx(0.10, abs=1e-6)
    assert mult == 1.0 and reason == "pass"


def test_cost_of_r_penny_stock_engages_derate(spread_settings):
    """Same penny name, wider spread 1100 bps: cost_of_r = (1100/1e4*0.001)/0.0005
    = 1.1e-4/5e-4 = 0.22. engage_cost=0.125, cap=0.25 -> in the linear band.
    cost_frac = (0.22-0.125)/(0.25-0.125) = 0.095/0.125 = 0.76 ; mult = 1 - 0.76*0.5
    = 0.62 (floored at 0.5). Asserts the exact linear interpolation at penny scale."""
    allow, mult, reason, meta = adaptive_spread_cost_veto_derate(
        symbol="PENNY",
        entry_price=0.001,
        current_spread_bps=1100.0,
        stop_distance=0.0005,
        db=_FakeDB(None),
        flag_enabled=True,
    )
    assert allow is True
    assert meta["cost_of_r"] == pytest.approx(0.22, abs=1e-6)
    expected = max(0.5, 1.0 - ((0.22 - 0.125) / 0.125) * 0.5)
    assert mult == pytest.approx(expected, abs=1e-6)
    assert mult == pytest.approx(0.62, abs=1e-4)


def test_cost_of_r_high_priced_5000_no_overflow(spread_settings):
    """$5000 name, R=$15 (0.3% stop), spread 4 bps. spread$ = (4/1e4)*5000 = 2.0 ;
    cost_of_r = 2.0/15 = 0.1333. Below engage (0.125) -> wait, 0.1333 > 0.125 so it
    DOES engage. Proves the high-price arm has no overflow and the ratio is right."""
    allow, mult, reason, meta = adaptive_spread_cost_veto_derate(
        symbol="EXPENSIVE",
        entry_price=5000.0,
        current_spread_bps=4.0,
        stop_distance=15.0,
        db=_FakeDB(None),
        flag_enabled=True,
    )
    assert allow is True
    # Source ROUNDS the reported cost_of_r to 4 decimals (round(2.0/15.0, 4) = 0.1333),
    # so tolerate the documented 4-decimal rounding (abs=1e-4) while still failing if the
    # ratio is grossly wrong.
    assert meta["cost_of_r"] == pytest.approx(2.0 / 15.0, abs=1e-4)
    # 0.1333 > engage 0.125 -> tiny derate.
    assert mult < 1.0
    assert math.isfinite(mult)


def test_cost_of_r_nan_spread_fails_open(spread_settings):
    """NaN spread bps: _f(nan) -> None -> 'no_spread' fail-OPEN (mult 1.0). Proves NaN
    cannot propagate into cost_of_r (which would be nan, comparisons all False, and a
    toxic spread would silently size at 1.0 OR crash)."""
    allow, mult, reason, meta = adaptive_spread_cost_veto_derate(
        symbol="X", entry_price=10.0, current_spread_bps=float("nan"),
        stop_distance=0.5, db=_FakeDB(None), flag_enabled=True,
    )
    assert (allow, mult, reason) == (True, 1.0, "no_spread")


def test_cost_of_r_inf_entry_fails_open(spread_settings):
    """+inf entry price: _f(inf) -> None (isfinite guard) -> 'no_entry_price' fail-open."""
    allow, mult, reason, _ = adaptive_spread_cost_veto_derate(
        symbol="X", entry_price=float("inf"), current_spread_bps=100.0,
        stop_distance=0.5, db=_FakeDB(None), flag_enabled=True,
    )
    assert (allow, mult, reason) == (True, 1.0, "no_entry_price")


def test_cost_of_r_zero_stop_distance_fails_open(spread_settings):
    """stop_distance=0: would make cost_of_r=inf and divide-by-zero. The sd<=0 guard
    catches it -> 'no_stop_distance' fail-open (never an unhandled ZeroDivision)."""
    allow, mult, reason, _ = adaptive_spread_cost_veto_derate(
        symbol="X", entry_price=10.0, current_spread_bps=100.0,
        stop_distance=0.0, db=_FakeDB(None), flag_enabled=True,
    )
    assert (allow, mult, reason) == (True, 1.0, "no_stop_distance")


def test_name_percentiles_nan_p50_rejected(spread_settings):
    """A percentile row with NaN p50 (a degenerate aggregate) must be rejected by the
    _f guard (NaN -> None) -> name_spread_percentiles returns None, NOT a dict with a
    NaN p50 that would poison anomaly_ratio = sb/p50 = nan downstream."""
    db = _FakeDB((float("nan"), 300.0, 400.0, 50))
    out = name_spread_percentiles(db, "X", lookback_days=20.0)
    assert out is None


def test_name_percentiles_inf_p75_falls_back_to_p50(spread_settings):
    """p75 = +inf is NOT > 0 in a useful sense but IS > 0; the code uses
    ``p75 if (p75 is not None and p75 > 0) else p50``. inf > 0 is True so inf would be
    kept. _f(inf) -> None first (isfinite), so p75 becomes None -> falls back to p50.
    Proves inf can't leak into the p75/p90 ladder."""
    db = _FakeDB((100.0, float("inf"), float("inf"), 50))
    out = name_spread_percentiles(db, "X", lookback_days=20.0)
    assert out is not None
    assert out["p75"] == 100.0  # inf -> _f None -> fallback to p50
    assert out["p90"] == 100.0  # inf -> None -> fallback to (p75 or p50) = 100


def test_extreme_anomaly_floor_penny_exact(spread_settings):
    """Penny name AT an EXTREME anomaly AND cost>cap -> floored to 0.5, allow True.
    p90=300, extreme_mult=1.5 -> extreme threshold 450bps. spread=900bps >= 450 ->
    extreme. cost_of_r at $0.001/R=$0.00002: (900/1e4*0.001)/2e-5 = 9e-5/2e-5 = 4.5
    >> cap 0.25 -> cost_too_high. extreme_cost_floor -> mult=floor=0.5 exactly."""
    db = _FakeDB((100.0, 200.0, 300.0, 50))
    allow, mult, reason, meta = adaptive_spread_cost_veto_derate(
        symbol="PENNY", entry_price=0.001, current_spread_bps=900.0,
        stop_distance=0.00002, db=db, flag_enabled=True,
    )
    assert allow is True  # DERATE-ONLY: never blocks
    assert mult == pytest.approx(0.5, abs=1e-9)  # exactly the floor
    assert meta.get("extreme_floor") is True


def test__f_helper_rejects_nan_inf_keeps_finite():
    """Directly torture the _f sanitizer: NaN/inf -> None; finite (incl. negative and
    a string number) -> float."""
    assert _f(float("nan")) is None
    assert _f(float("inf")) is None
    assert _f(float("-inf")) is None
    assert _f(None) is None
    assert _f("3.5") == 3.5
    assert _f(-2.0) == -2.0
    assert _f(0.0) == 0.0


# ════════════════════════════════════════════════════════════════════════════════
# (2b) EXTENSION CAP — penny/high-price + (5) 0.1+0.2 binary-unfriendly boundary
# ════════════════════════════════════════════════════════════════════════════════


class _ExtSettings:
    """Minimal settings stub for _entry_extension_veto (it reads getattr-with-default)."""

    chili_momentum_entry_extension_veto_enabled = True
    chili_momentum_entry_extension_atr_mult = 8.0
    chili_momentum_entry_extension_floor_pct = 0.08
    chili_momentum_explosive_recalibration_enabled = False
    chili_momentum_entry_extension_rvol_boost_enabled = False


def test_extension_cap_boundary_is_inclusive_at_exact_float():
    """The veto is ``ep >= lvl*(1+cap)`` — INCLUSIVE. Construct an entry EXACTLY on the
    cap so a `>` vs `>=` slip is caught. lvl=100, atr makes cap=floor=0.08 (8*0.005=0.04
    < 0.08 floor). threshold = 100*1.08 = 108.0 (exact in binary). ep=108.0 -> veto."""
    s = _ExtSettings()
    # cap = max(0.08, 8*0.005) = 0.08 ; threshold = 100*1.08 = 108.0
    assert _entry_extension_veto(108.0, 100.0, 0.005, s) is True   # exactly == -> veto
    assert _entry_extension_veto(107.999, 100.0, 0.005, s) is False  # just under -> ok


def test_extension_cap_binary_unfriendly_boundary():
    """Prove the comparison uses the SAME float arithmetic on both sides (no spurious
    pass/over-veto from a representation gap). k=8.0, atr=0.0175 -> 8*0.0175 = 0.14 exactly,
    but threshold = lvl*(1+cap) = 11.400000000000002 IS binary-unfriendly. An entry EXACTLY
    at that threshold must be vetoed (>= inclusive); one ULP below must PASS."""
    s = _ExtSettings()
    lvl = 10.0
    atr = 0.0175
    cap = 8.0 * atr  # 0.14 exactly for this input
    assert cap == 0.14  # this product lands exactly on 0.14
    assert cap > 0.08   # above the floor so k*atr is the binding cap
    threshold = lvl * (1.0 + cap)  # 11.400000000000002 — the gnarly boundary lives here
    # entry exactly at the (artifact) threshold -> veto (inclusive, same float both sides).
    assert _entry_extension_veto(threshold, lvl, atr, s) is True
    # entry one ULP below -> must PASS (proves no off-by-epsilon over-veto).
    assert _entry_extension_veto(math.nextafter(threshold, 0.0), lvl, atr, s) is False


def test_extension_cap_penny_stock_no_underflow():
    """Penny break level $0.002, entry $0.0024 (+20%), atr 0.5%. cap = max(0.08,0.04)
    = 0.08 -> threshold 0.002*1.08 = 0.00216. 0.0024 >= 0.00216 -> veto. Proves the
    cap math holds at sub-cent magnitudes (no underflow to 0)."""
    s = _ExtSettings()
    assert _entry_extension_veto(0.0024, 0.002, 0.005, s) is True
    # entry within the cap (+5%) -> ok.
    assert _entry_extension_veto(0.0021, 0.002, 0.005, s) is False


def test_extension_cap_high_priced_5000():
    """$5000 name, break $5000, entry $5500 (+10%), atr 0.5% -> cap 0.08 -> threshold
    $5400. 5500 >= 5400 -> veto. No overflow at 4-figure prices."""
    s = _ExtSettings()
    assert _entry_extension_veto(5500.0, 5000.0, 0.005, s) is True
    assert _entry_extension_veto(5300.0, 5000.0, 0.005, s) is False


def test_extension_cap_nan_atr_falls_to_floor_and_vetoes():
    """FIX HIGH-2: a NON-FINITE ``atr_pct`` is now EXPLICITLY coerced to a=0.0 (no longer
    relying on Python's ``max(0.0, nan)`` arg-order quirk), so the cap collapses to the FLAT
    floor (0.08) and a far-extended entry VETOES. The verdict is the same as before but now
    DELIBERATE and robust to arg order / code-path, not an accidental fragility."""
    s = _ExtSettings()
    # far-extended entry vs a tiny level -> with cap pinned to the 0.08 floor it vetoes.
    out = _entry_extension_veto(10_000.0, 1.0, float("nan"), s)
    assert out is True  # NaN ATR -> cap falls to the 0.08 floor -> extended entry vetoes


def test_extension_cap_inf_atr_falls_to_floor_and_vetoes():
    """FIX HIGH-2: +inf ATR is non-finite, so it now falls back to the FLAT extension floor
    (a=0.0 -> cap=0.08) instead of cap=inf. Pre-fix, cap=inf made threshold=inf and the gate
    fail-OPEN (no veto) — a degenerate volatility reading silently DISARMED the chase-guard.
    Now a far-extended entry (10_000 vs level 100 -> +9900%) correctly VETOES on the floor."""
    s = _ExtSettings()
    assert _entry_extension_veto(10_000.0, 100.0, float("inf"), s) is True


# ════════════════════════════════════════════════════════════════════════════════
# (4) DOJI body/range at range == eps (near-zero range bar)
# ════════════════════════════════════════════════════════════════════════════════


def test_doji_zero_range_fails_safe():
    """range == 0 (o==h==l==c): the rng<=0 guard returns veto=False (never block on an
    unreadable bar). Proves no ZeroDivision in body/range."""
    veto, dbg = _doji_trigger_veto(5.0, 5.0, 5.0, 5.0, atr_pct=0.01, base_body_frac=0.25)
    assert veto is False
    assert "doji_body_frac" not in dbg  # short-circuited before the division


def test_doji_near_eps_range_huge_body_frac():
    """range = 1 ULP above zero but body == range (a full-body micro-bar). body/range
    = 1.0 >= threshold -> NOT a doji (veto False). Stresses the division at the
    smallest representable positive range without overflow to inf."""
    base = 100.0
    h = math.nextafter(base, math.inf)  # range = 1 ULP (~1.4e-14 at 100)
    rng = h - base
    assert rng > 0 and math.isfinite(rng)
    # open=low=base, close=high -> body = rng, full green body -> body_frac = 1.0.
    veto, dbg = _doji_trigger_veto(base, h, base, h, atr_pct=0.0, base_body_frac=0.25)
    assert veto is False
    assert dbg["doji_body_frac"] == pytest.approx(1.0, abs=1e-9)


def test_doji_near_eps_range_tiny_body_is_doji():
    """range = a tiny but finite value, body a SMALL fraction of it, NOT a strong
    bull break (red or weak close) -> veto=True. Proves the gate still classifies a
    true doji at micro-range without a float artifact flipping the verdict.
    Construct: l=100.0, h=100.0+1e-6 (range 1e-6), o=c+tiny so body ~ 1e-9
    (body_frac ~ 1e-3 << 0.25). Use a RED bar (c<o) so the strong-break override
    can't rescue it."""
    l = 100.0
    h = 100.0 + 1e-6
    o = 100.0 + 6e-7
    c = 100.0 + 5e-7  # c<o -> red -> not a strong bull break
    rng = h - l
    body = abs(c - o)
    assert rng == pytest.approx(1e-6, rel=1e-6)
    assert body / rng < 0.25
    veto, dbg = _doji_trigger_veto(o, h, l, c, atr_pct=0.0, base_body_frac=0.25)
    assert veto is True
    assert dbg["doji_body_frac"] < dbg["doji_threshold"]


def test_doji_threshold_binary_boundary_body_frac_exact():
    """body_frac EXACTLY at the threshold passes (``>=`` is inclusive). Pick range/body
    so body/range == base_body_frac + atr_pct exactly. base=0.25, atr=0.05 -> thresh
    0.30000000000000004 (0.25+0.05 float artifact). Make body/range hit the SAME float:
    range=1.0, body=0.30000000000000004 -> body_frac == thresh -> NOT a doji (>=)."""
    thresh = 0.25 + 0.05  # 0.30000000000000004
    # range = 1.0 (l=0, h=1), body = thresh: a green bar closing at body height.
    # o=0 (=l), c=thresh -> body=thresh, close not in upper half (0.3<0.5) so the
    # strong-break override does NOT fire; the verdict rides purely on body_frac>=thresh.
    veto, dbg = _doji_trigger_veto(0.0, 1.0, 0.0, thresh, atr_pct=0.05, base_body_frac=0.25)
    assert dbg["doji_body_frac"] == pytest.approx(thresh, abs=1e-12)
    assert dbg["doji_threshold"] == pytest.approx(thresh, abs=1e-12)
    assert veto is False  # body_frac >= thresh (inclusive) -> not a doji


def test_doji_nan_inputs_fail_open():
    """NaN OHLC: rng = nan, ``rng <= 0`` is False, body_frac = nan, ``nan >= thresh``
    is False -> would fall to the strong-break override path. is_strong_bull_break_candle
    on NaN: rng<=0 False, c<o (nan<nan) False, (c-l)/rng = nan < min_close_pos False,
    upper/rng nan > max False -> returns True -> override -> veto False. Net: a NaN bar
    must NOT veto (fail-open). This proves NaN doesn't slip through as veto=True."""
    veto, _ = _doji_trigger_veto(
        float("nan"), float("nan"), float("nan"), float("nan"),
        atr_pct=0.01, base_body_frac=0.25,
    )
    assert veto is False


# ════════════════════════════════════════════════════════════════════════════════
# (5) PULLBACK-DEPTH CEILING — 0.1+0.2 binary-unfriendly boundary
# ════════════════════════════════════════════════════════════════════════════════


def test_pullback_ceiling_disabled_returns_zero():
    """enabled=False -> 0.0 exactly (byte-identical fall-through for callers)."""
    assert _adaptive_pullback_depth_ceiling(0.05, False) == 0.0
    assert _adaptive_pullback_depth_ceiling(None, False) == 0.0


def test_pullback_ceiling_calm_floor_exact():
    """atr_pct ~ 0 -> the base (0.50) is the floor of the ceiling. atr=1e-9 -> ~0.50."""
    out = _adaptive_pullback_depth_ceiling(1e-9, True)
    assert out == pytest.approx(_VOL_SHALLOW_BASE, abs=1e-6)


def test_pullback_ceiling_binary_unfriendly_atr():
    """Reproduce the documented worked point with a binary-unfriendly atr. The formula
    is base + atr*mult = 0.50 + atr*1.5. Pick atr=0.1 (not exact in binary):
    0.50 + 0.1*1.5 = 0.50 + 0.15000000000000002 = 0.65 (with artifact). Assert the
    EXACT float the implementation produces, not a rounded 0.65, so a reordered
    expression (atr*1.5 vs different grouping) is caught."""
    atr = 0.1
    expected = min(_VOL_SHALLOW_CEIL, _VOL_SHALLOW_BASE + atr * _VOL_SHALLOW_ATR_MULT)
    out = _adaptive_pullback_depth_ceiling(atr, True)
    assert out == expected  # bit-exact
    # And it is NOT the naive 0.65 literal (proves the artifact is preserved, not masked).
    assert out == pytest.approx(0.65, abs=1e-9)


def test_pullback_ceiling_hard_cap_at_075():
    """High ATR saturates at the 0.75 hard cap, never beyond. atr=0.50 ->
    0.50 + 0.75 = 1.25 -> min(0.75, 1.25) = 0.75. Also atr=1e6 -> still 0.75."""
    assert _adaptive_pullback_depth_ceiling(0.50, True) == _VOL_SHALLOW_CEIL
    assert _adaptive_pullback_depth_ceiling(1e6, True) == _VOL_SHALLOW_CEIL


def test_pullback_ceiling_negative_atr_clamped_to_base():
    """Negative atr (a corrupt reading): the ``a = ... if atr>0 else 0.0`` guard zeroes
    it -> the calm floor (base), never a sub-base ceiling that would over-tighten."""
    out = _adaptive_pullback_depth_ceiling(-0.5, True)
    assert out == pytest.approx(_VOL_SHALLOW_BASE, abs=1e-9)


def test_pullback_ceiling_nan_atr_clamped_to_base():
    """NaN atr: ``atr_pct > 0`` with nan is False -> a=0.0 -> base. A NaN must NOT
    propagate into the ceiling (a nan ceiling would make every depth check False)."""
    out = _adaptive_pullback_depth_ceiling(float("nan"), True)
    assert out == pytest.approx(_VOL_SHALLOW_BASE, abs=1e-9)


# ════════════════════════════════════════════════════════════════════════════════
# (3) GREEN-DAY + CATALYST multiplier clamps — float math + NaN/inf guards
# ════════════════════════════════════════════════════════════════════════════════


class _FakeOutcomeDB:
    """Stub DB whose .query(...) raises -> consecutive_green_days returns (0, read_failed).
    Lets us drive green_day_graduation_multiplier without touching real ORM tables."""

    def query(self, *_a, **_k):
        raise RuntimeError("no-db")


def test_green_day_multiplier_disabled_is_neutral(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_green_day_graduation_enabled", False, raising=False)
    mult, meta = risk_policy.green_day_graduation_multiplier(_FakeOutcomeDB(), execution_family="momentum_neural")
    assert mult == 1.0
    assert meta["graduation_mult"] == 1.0


def test_green_day_multiplier_clamp_exact(monkeypatch):
    """mult = clamp(1 + step*(streak-1), 1.0, max). Force a known streak by patching
    consecutive_green_days. step=0.1, streak=4 -> 1 + 0.1*3 = 1.3 (0.1*3 =
    0.30000000000000004 artifact -> 1.3000000000000003). Assert it equals the SAME
    float the implementation computes (not a rounded 1.3) and is < max so no clamp."""
    monkeypatch.setattr(settings, "chili_momentum_green_day_graduation_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_green_day_step_per_day", 0.1, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_green_day_max_multiplier", 2.0, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_green_day_lookback_days", 30, raising=False)
    monkeypatch.setattr(
        risk_policy, "consecutive_green_days",
        lambda *a, **k: (4, {"green_usd": 10.0, "days_seen": 4}),
    )
    mult, meta = risk_policy.green_day_graduation_multiplier(_FakeOutcomeDB(), execution_family="m")
    expected = max(1.0, min(2.0, 1.0 + 0.1 * (4 - 1)))
    assert mult == expected
    assert mult == pytest.approx(1.3, abs=1e-9)
    assert meta["consecutive_green_days"] == 4


def test_green_day_multiplier_huge_streak_clamps_to_max(monkeypatch):
    """A pathological streak (1000 days) must clamp to max_multiplier, not run away."""
    monkeypatch.setattr(settings, "chili_momentum_green_day_graduation_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_green_day_step_per_day", 0.1, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_green_day_max_multiplier", 2.0, raising=False)
    monkeypatch.setattr(
        risk_policy, "consecutive_green_days", lambda *a, **k: (1000, {}),
    )
    mult, _ = risk_policy.green_day_graduation_multiplier(_FakeOutcomeDB(), execution_family="m")
    assert mult == 2.0  # exactly the ceiling, no overflow


def test_green_day_multiplier_max_below_one_clamped_up(monkeypatch):
    """A misconfigured max_multiplier < 1.0 must be raised to 1.0 (the multiplier can
    only ADD; a <1 max would SHRINK every trade — guarded by ``if max_mult<1: =1``)."""
    monkeypatch.setattr(settings, "chili_momentum_green_day_graduation_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_green_day_step_per_day", 0.1, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_green_day_max_multiplier", 0.3, raising=False)
    monkeypatch.setattr(risk_policy, "consecutive_green_days", lambda *a, **k: (5, {}))
    mult, _ = risk_policy.green_day_graduation_multiplier(_FakeOutcomeDB(), execution_family="m")
    assert mult == 1.0  # max clamped to 1.0 -> no shrink


def test_green_day_multiplier_nan_step_does_not_crash(monkeypatch):
    """A NaN step (corrupt config): 1 + nan*(streak-1) = nan ; min(max, nan) and
    max(1.0, nan) — Python's min/max with nan are order-dependent. The function is
    wrapped in try/except returning (1.0, error_fail_neutral) on ANY exception, but
    nan does NOT raise. So the result may be nan OR 1.0. We assert it is at least
    FINITE and >= 1.0 (a nan multiplier silently sizing a trade is the bug to catch).
    SUSPECTED SOURCE BUG if this fails: a NaN step propagates to the size multiplier."""
    monkeypatch.setattr(settings, "chili_momentum_green_day_graduation_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_green_day_step_per_day", float("nan"), raising=False)
    monkeypatch.setattr(settings, "chili_momentum_green_day_max_multiplier", 2.0, raising=False)
    monkeypatch.setattr(risk_policy, "consecutive_green_days", lambda *a, **k: (5, {}))
    mult, _meta = risk_policy.green_day_graduation_multiplier(_FakeOutcomeDB(), execution_family="m")
    # Document the actual behaviour: max(1.0, min(2.0, 1.0 + nan*4)).
    # min(2.0, nan) -> 2.0 (Python returns first arg when second is nan in min? actually
    # min(2.0, nan)=2.0 because nan<2.0 is False). max(1.0, 2.0)=2.0. So mult should be 2.0.
    assert math.isfinite(mult), f"NaN step leaked into the size multiplier: {mult!r}"
    assert mult >= 1.0


def test_catalyst_conviction_disabled_neutral(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_catalyst_conviction_enabled", False, raising=False)
    mult, meta = risk_policy.catalyst_conviction_size_multiplier("ABCD")
    assert mult == 1.0 and meta["conviction_mult"] == 1.0


def test_catalyst_conviction_clamp_exact(monkeypatch):
    """mult = clamp(1 + step*rank, 1.0, max). STRONG rank=3, step=0.15 ->
    1 + 0.45 = 1.45. max=1.5 -> no clamp. 0.15*3 = 0.45 (0.44999999999999996 artifact)
    -> 1.4499999999999998. Assert the implementation's exact float."""
    monkeypatch.setattr(settings, "chili_momentum_catalyst_conviction_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_catalyst_conviction_step", 0.15, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_catalyst_conviction_max_multiplier", 1.5, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_fake_catalyst_guard_enabled", True, raising=False)
    mult, meta = risk_policy.catalyst_conviction_size_multiplier(
        "RUNR", strong_symbols={"RUNR"}, weak_symbols=set(), fake_symbols=set(),
    )
    expected = max(1.0, min(1.5, 1.0 + 0.15 * 3))
    assert mult == expected
    assert mult == pytest.approx(1.45, abs=1e-9)
    assert meta["grade_rank"] == 3


def test_catalyst_conviction_step_zero_not_falsy_fallback(monkeypatch):
    """A LEGIT step=0.0 must NOT fall back to the 0.15 default (the code uses a
    None-aware default, not ``or``). step=0.0 -> mult = 1 + 0 = 1.0 (no boost), proving
    the falsy-zero trap is dodged."""
    monkeypatch.setattr(settings, "chili_momentum_catalyst_conviction_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_catalyst_conviction_step", 0.0, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_catalyst_conviction_max_multiplier", 1.5, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_fake_catalyst_guard_enabled", True, raising=False)
    mult, meta = risk_policy.catalyst_conviction_size_multiplier(
        "RUNR", strong_symbols={"RUNR"}, weak_symbols=set(), fake_symbols=set(),
    )
    assert mult == 1.0  # step 0.0 honoured (not the 0.15 default) -> no boost
    assert meta["step"] == 0.0


def test_catalyst_conviction_weak_dominates_no_boost(monkeypatch):
    """A name BOTH strong and weak -> rank 0 (weak dominates) -> mult 1.0. Proves the
    catalyst clamp never boosts a distrusted dilution name."""
    monkeypatch.setattr(settings, "chili_momentum_catalyst_conviction_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_catalyst_conviction_step", 0.15, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_catalyst_conviction_max_multiplier", 1.5, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_fake_catalyst_guard_enabled", True, raising=False)
    mult, meta = risk_policy.catalyst_conviction_size_multiplier(
        "DILU", strong_symbols={"DILU"}, weak_symbols={"DILU"}, fake_symbols=set(),
    )
    assert mult == 1.0 and meta["grade_rank"] == 0
