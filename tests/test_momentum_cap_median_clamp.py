"""ME-1: rolling-median spike guard for momentum per-trade caps.

A transient bad per-venue equity read (e.g. a Coinbase get_portfolio spike) inflates
BOTH equity-relative per-trade caps at once, releasing the notional ceiling and
4-6x-ing size + risk (FIDA/KAIO oversized trades = ~60% of the halting daily loss,
2026-06-06). ``bounded_by_rolling_median`` clamps each frozen cap DOWN to a bounded
multiple of its rolling median across recent same-venue admissions.
docs/DESIGN/MOMENTUM_LANE_ENTRY_STOP_REALIGNMENT.md
"""

from __future__ import annotations

from app.services.trading.momentum_neural.risk_policy import bounded_by_rolling_median


def test_spike_clamps_to_multiple_times_median():
    # 40 healthy admissions around $26, then a 6x equity-read spike asks for $156.
    history = [26.0] * 40
    value, deriv = bounded_by_rolling_median(156.0, history, multiple=2.0)
    assert deriv["clamped"] is True
    assert deriv["median"] == 26.0
    assert value == 52.0  # 2.0 x median, not 156
    assert deriv["raw"] == 156.0


def test_within_bound_passes_through_unclamped():
    # Legitimate gradual growth: cap is below 2x the rolling median -> no clamp.
    history = [26.0] * 20
    value, deriv = bounded_by_rolling_median(40.0, history, multiple=2.0)
    assert deriv["clamped"] is False
    assert deriv["reason"] == "within_bound"
    assert value == 40.0


def test_thin_history_never_clamps():
    # Below the n>=5 evidence floor the median is untrusted; raw passes through.
    value, deriv = bounded_by_rolling_median(999.0, [26.0, 26.0], multiple=2.0)
    assert deriv["clamped"] is False
    assert deriv["reason"] == "thin_history"
    assert value == 999.0


def test_median_resists_outliers_in_history():
    # Even if two prior spiked caps leaked into history, the median ignores them, so a
    # fresh spike still clamps to the healthy center.
    history = [26.0] * 38 + [107.0, 156.0]
    value, deriv = bounded_by_rolling_median(150.0, history, multiple=2.0)
    assert deriv["median"] == 26.0
    assert deriv["clamped"] is True
    assert value == 52.0


def test_nonpositive_raw_is_preserved_as_disable():
    # A 0/negative cap is a deliberate operator disable/block — never resurrected.
    value, deriv = bounded_by_rolling_median(0.0, [26.0] * 10, multiple=2.0)
    assert value == 0.0
    assert deriv["clamped"] is False
    assert deriv["reason"] == "nonpositive_or_disabled"


def test_multiple_below_one_is_coerced_to_one():
    # A misconfigured multiple < 1 would clamp below the median; coerce to 1.0 so the
    # guard never clamps a cap below its own rolling center.
    history = [26.0] * 10
    value, deriv = bounded_by_rolling_median(100.0, history, multiple=0.5)
    assert deriv["multiple"] == 1.0
    assert value == 26.0  # 1.0 x median


def test_nonpositive_median_skips_clamp():
    # Degenerate history (all non-positive filtered upstream, but guard defensively).
    value, deriv = bounded_by_rolling_median(100.0, [0.0, 0.0, 0.0, 0.0, 0.0, 0.0], multiple=2.0)
    assert deriv["clamped"] is False
    assert deriv["reason"] == "nonpositive_median"
    assert value == 100.0
