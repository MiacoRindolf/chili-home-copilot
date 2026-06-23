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
