"""Phase G - unit tests for pure ``compute_bracket_intent``.

Covers long / short, ATR present vs missing, lifecycle tightening,
regime tightening, low-win-rate tightening, determinism, and input
validation. No DB access.
"""
from __future__ import annotations

import pytest

from app.services.trading.bracket_intent import (
    BracketIntentInput,
    BracketIntentResult,
    compute_bracket_intent,
)


def _base_input(**over) -> BracketIntentInput:
    defaults = dict(
        ticker="AAPL",
        direction="long",
        entry_price=100.0,
        quantity=10.0,
        atr=2.0,
        stop_model="atr_swing",
        pattern_name=None,
        pattern_id=None,
        lifecycle_stage="validated",
        pattern_win_rate=None,
        regime="cautious",
    )
    defaults.update(over)
    return BracketIntentInput(**defaults)


def test_long_atr_swing_normal_regime_gives_stop_below_and_target_above():
    res = compute_bracket_intent(_base_input())
    assert isinstance(res, BracketIntentResult)
    assert res.stop_price is not None and res.target_price is not None
    assert res.stop_price < 100.0
    assert res.target_price > 100.0
    # With ATR=2.0, atr_swing stop_mult_normal=2.0 => stop ~= 96.0
    assert abs(res.stop_price - 96.0) < 0.5
    # target_mult=3.0 => target ~= 106.0
    assert abs(res.target_price - 106.0) < 0.5


def test_short_direction_inverts_stop_and_target():
    res = compute_bracket_intent(_base_input(direction="short"))
    assert res.stop_price > 100.0
    assert res.target_price < 100.0


def test_missing_atr_falls_back_to_pct_stop():
    res = compute_bracket_intent(_base_input(atr=None))
    assert res.stop_price is not None
    # 8% pct fallback for long => ~ 92.0
    assert abs(res.stop_price - 92.0) < 0.01


def test_risk_off_regime_tightens_stop_vs_cautious():
    base = compute_bracket_intent(_base_input(regime="cautious"))
    tighter = compute_bracket_intent(_base_input(regime="risk_off"))
    # Long + risk_off => stop closer to entry (higher stop price)
    assert tighter.stop_price > base.stop_price


def test_risk_on_regime_gives_more_room_vs_cautious():
    base = compute_bracket_intent(_base_input(regime="cautious"))
    loose = compute_bracket_intent(_base_input(regime="risk_on"))
    assert loose.stop_price < base.stop_price


def test_decayed_lifecycle_tightens_stop():
    base = compute_bracket_intent(_base_input(lifecycle_stage="validated"))
    decayed = compute_bracket_intent(_base_input(lifecycle_stage="decayed"))
    assert decayed.stop_price > base.stop_price


def test_low_win_rate_tightens_stop():
    normal = compute_bracket_intent(_base_input(pattern_win_rate=0.60))
    tight = compute_bracket_intent(_base_input(pattern_win_rate=0.35))
    assert tight.stop_price > normal.stop_price


def test_stop_mult_override_is_respected():
    res = compute_bracket_intent(_base_input(stop_mult_override=1.0))
    # atr=2.0, mult_override=1.0, other factors cautious=1.0 validated=1.0
    # => stop_price ~= 98.0
    assert abs(res.stop_price - 98.0) < 0.1


def test_crypto_ticker_uses_8_decimal_rounding():
    res = compute_bracket_intent(_base_input(ticker="ZK-USD", entry_price=0.12345678, atr=0.001))
    s = str(res.stop_price)
    if "." in s:
        assert len(s.split(".")[1]) <= 8


def test_determinism_same_input_same_output():
    inp = _base_input(pattern_id=123, pattern_name="bull_flag")
    a = compute_bracket_intent(inp)
    b = compute_bracket_intent(inp)
    assert a == b


def test_unknown_direction_raises():
    with pytest.raises(ValueError):
        compute_bracket_intent(_base_input(direction="sideways"))


def test_non_positive_entry_raises():
    with pytest.raises(ValueError):
        compute_bracket_intent(_base_input(entry_price=0.0))


def test_non_positive_quantity_raises():
    with pytest.raises(ValueError):
        compute_bracket_intent(_base_input(quantity=0.0))


def test_reasoning_includes_core_tags():
    res = compute_bracket_intent(_base_input(regime="risk_on"))
    assert "direction=long" in res.reasoning
    assert "regime=risk_on" in res.reasoning
    assert "atr=" in res.reasoning


def test_brain_summary_includes_regime_and_lifecycle_factors():
    res = compute_bracket_intent(
        _base_input(regime="risk_off", lifecycle_stage="decayed")
    )
    assert res.brain_summary.get("regime") == "risk_off"
    assert res.brain_summary.get("lifecycle") == "decayed"
