"""L2.2 liquidity-scaled risk cap — shrink per-trade RISK as the live spread eats the
name's ADAPTIVE spread tolerance (wide-spread/illiquid names sized DOWN, never rejected;
the surgical fix the L3 entry filter wasn't — it never kills a trade or winner). Pure
helper: monotonic, bounded [floor, 1.0], fail-neutral. The end-to-end relative size-shrink
is proven by the 2026-06-22 replay A/B (live + replay share the helper => parity)."""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural.risk_policy import spread_liquidity_risk_multiplier

# explicit ratio + abs_cap so the test is deterministic regardless of live settings.


def test_tight_spread_full_size():
    m, _ = spread_liquidity_risk_multiplier(0.01, 200.0, ratio=0.5, abs_cap_bps=800.0)
    assert m == pytest.approx(1.0, abs=0.01)


def test_very_wide_spread_floors():
    m, _ = spread_liquidity_risk_multiplier(1e6, 200.0, floor=0.5, ratio=0.5, abs_cap_bps=800.0)
    assert m == pytest.approx(0.5)  # clamped to the floor, never below


def test_monotonic_wider_is_smaller_or_equal():
    ms = [spread_liquidity_risk_multiplier(s, 200.0, ratio=0.5, abs_cap_bps=800.0)[0]
          for s in (10.0, 50.0, 100.0, 200.0)]
    assert ms == sorted(ms, reverse=True)  # non-increasing in spread


def test_mid_spread_between_floor_and_one():
    m, _ = spread_liquidity_risk_multiplier(50.0, 200.0, floor=0.5, ratio=0.5, abs_cap_bps=800.0)
    assert 0.5 <= m <= 1.0


def test_qxl_like_wide_name_is_shrunk_not_rejected():
    # 119bps (QXL-like) eats most of a modest tolerance => shrunk, but never below floor
    # and never 0 (the position survives — it is sized down, not rejected).
    m, meta = spread_liquidity_risk_multiplier(119.0, 120.0, floor=0.5, ratio=0.5, abs_cap_bps=800.0)
    assert 0.5 <= m < 1.0
    assert meta["mult"] == pytest.approx(m)


def test_fail_neutral_on_bad_inputs():
    for bad in (None, 0.0, -5.0, float("nan")):
        assert spread_liquidity_risk_multiplier(bad, 200.0)[0] == 1.0  # never increases risk


def test_floor_out_of_range_defaults_to_half():
    m, meta = spread_liquidity_risk_multiplier(1e6, 200.0, floor=5.0, ratio=0.5, abs_cap_bps=800.0)
    assert m == pytest.approx(0.5)
    assert meta["floor"] == 0.5


# ── STEP-E #15: EM-scaled absolute spread cap ────────────────────────────────

from app.services.trading.momentum_neural.risk_policy import adaptive_max_spread_bps


def test_em_scaled_cap_admits_legitimately_wide_lowfloat():
    """DSY-class: expected move justifies a 721bps adaptive ceiling (ratio 0.5 * em 1442).
    The FIXED 300 cap clamps it to 300 (the bug); the EM-scaled cap (k=1.0) admits 721."""
    # Legacy fixed cap clamps to 300.
    assert adaptive_max_spread_bps(12.0, 1442.0, 0.5, abs_cap_bps=300.0) == pytest.approx(300.0)
    # EM-scaled cap: effective_cap = max(300, 1.0 * 721) = 721 -> not clamped.
    scaled = adaptive_max_spread_bps(12.0, 1442.0, 0.5, abs_cap_bps=300.0, abs_cap_em_scale_k=1.0)
    assert scaled == pytest.approx(721.0)


def test_em_scaled_cap_still_blocks_junk_wide_spread():
    """A junk name has a SMALL expected move, so its EM ceiling (ratio*em) is small and the
    scaled cap stays at the fixed 300 — a 2000bps spread is still above tolerance (blocks)."""
    # em=400 -> ratio*em=200; effective_cap = max(300, 1.0*200) = 300; adaptive = min(200,300)=200.
    tol = adaptive_max_spread_bps(12.0, 400.0, 0.5, abs_cap_bps=300.0, abs_cap_em_scale_k=1.0)
    assert tol == pytest.approx(200.0)
    assert 2000.0 > tol  # a 2000bps spread exceeds tolerance -> rejected downstream


def test_em_scaled_cap_couples_to_proportional_size_down():
    """Acceptance of the wider spread is COUPLED to a size-down: a DSY-class name at its
    721bps EM ceiling shrinks toward the floor; a tight name barely shrinks. The multiplier
    uses the EM-scaled tolerance internally (settings enabled by default)."""
    wide, _ = spread_liquidity_risk_multiplier(721.0, 1442.0, floor=0.5, abs_cap_bps=300.0)
    tight, _ = spread_liquidity_risk_multiplier(50.0, 1442.0, floor=0.5, abs_cap_bps=300.0)
    assert wide == pytest.approx(0.5)   # eats its full (721bps) tolerance -> floor
    assert tight > 0.85                 # tiny spread vs a wide tolerance -> near full size


def test_em_scale_none_preserves_legacy_fixed_cap():
    """k=None (legacy) keeps the fixed abs cap — byte-identical to the prior behavior."""
    assert adaptive_max_spread_bps(12.0, 1442.0, 0.5, abs_cap_bps=300.0, abs_cap_em_scale_k=None) == pytest.approx(300.0)


def test_em_scaled_cap_fail_closed_on_missing_em():
    """Missing expected move => the adaptive value collapses to the base floor (fail-closed;
    the EM-scale never relaxes without an EM to justify it)."""
    assert adaptive_max_spread_bps(12.0, None, 0.5, abs_cap_bps=300.0, abs_cap_em_scale_k=1.0) == pytest.approx(12.0)
