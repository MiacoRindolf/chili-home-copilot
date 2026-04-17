"""Pure unit tests for :mod:`app.services.trading.position_sizer_model`.

No DB, no broker, no network. These tests lock the Kelly math,
cap behavior, and determinism contract Phase H depends on.
"""
from __future__ import annotations

import pytest

from app.services.trading.position_sizer_model import (
    CorrelationBudget,
    PortfolioBudget,
    PositionSizerInput,
    compute_proposal,
    compute_proposal_id,
)


def _default_input(**overrides) -> PositionSizerInput:
    base = {
        "ticker": "AAPL",
        "direction": "long",
        "asset_class": "equity",
        "entry_price": 100.0,
        "stop_price": 95.0,     # 5% stop
        "capital": 100_000.0,
        "calibrated_prob": 0.60,
        "payoff_fraction": 0.10,   # 10% target
        "loss_per_unit": 0.05,     # 5% stop
        "cost_fraction": 0.0,
        "kelly_scale": 0.25,
        "max_risk_pct": 2.0,
        "equity_bucket_cap_pct": 15.0,
        "crypto_bucket_cap_pct": 10.0,
        "single_ticker_cap_pct": 7.5,
        "qty_rounding": "int",
    }
    base.update(overrides)
    return PositionSizerInput(**base)


# ---------------------------------------------------------------------------
# Core Kelly math
# ---------------------------------------------------------------------------


def test_positive_edge_produces_positive_size():
    # Use a modest-edge input so the risk cap does not fire and we can
    # verify raw Kelly -> scaled Kelly relationship directly.
    out = compute_proposal(
        inp=_default_input(
            calibrated_prob=0.52, payoff_fraction=0.05, loss_per_unit=0.05,
        ),
        source="unit",
    )
    assert out.proposed_notional > 0
    assert out.proposed_quantity > 0
    assert out.kelly_fraction > 0
    assert out.kelly_scaled_fraction > 0
    # Quarter-Kelly: scaled = raw * 0.25 when no cap trims the fraction.
    assert out.kelly_scaled_fraction == pytest.approx(out.kelly_fraction * 0.25)
    assert out.expected_net_pnl > 0
    assert out.reasoning.get("risk_cap_triggered") is False


def test_negative_edge_returns_zero():
    # p*W - q*L < 0 means negative edge. 45% prob with 2:1 payoff is still
    # net-negative after costs.
    out = compute_proposal(
        inp=_default_input(calibrated_prob=0.30, cost_fraction=0.01),
        source="unit",
    )
    assert out.proposed_notional == 0.0
    assert out.proposed_quantity == 0.0
    assert out.proposed_risk_pct == 0.0


def test_zero_win_probability_returns_zero():
    out = compute_proposal(
        inp=_default_input(calibrated_prob=0.0),
        source="unit",
    )
    assert out.proposed_notional == 0.0


def test_invalid_prices_return_zero():
    out = compute_proposal(inp=_default_input(entry_price=0.0), source="unit")
    assert out.proposed_notional == 0.0
    assert out.reasoning.get("reject_reason") == "invalid_prices_or_capital"

    out = compute_proposal(inp=_default_input(stop_price=100.0), source="unit")
    assert out.proposed_notional == 0.0

    out = compute_proposal(inp=_default_input(capital=-1.0), source="unit")
    assert out.proposed_notional == 0.0


def test_higher_probability_increases_kelly():
    base = compute_proposal(inp=_default_input(calibrated_prob=0.55), source="unit")
    better = compute_proposal(inp=_default_input(calibrated_prob=0.70), source="unit")
    assert better.kelly_fraction > base.kelly_fraction
    assert better.proposed_notional >= base.proposed_notional


def test_wider_payoff_increases_kelly():
    base = compute_proposal(inp=_default_input(payoff_fraction=0.05), source="unit")
    wider = compute_proposal(inp=_default_input(payoff_fraction=0.20), source="unit")
    assert wider.kelly_fraction > base.kelly_fraction


# ---------------------------------------------------------------------------
# Risk cap
# ---------------------------------------------------------------------------


def test_risk_cap_trims_kelly_when_loss_fraction_is_small():
    # With a 1% stop and high edge, raw kelly scaled would exceed the
    # 2% max-risk envelope.
    out = compute_proposal(
        inp=_default_input(
            loss_per_unit=0.01,
            stop_price=99.0,
            payoff_fraction=0.05,
            calibrated_prob=0.70,
            max_risk_pct=2.0,
        ),
        source="unit",
    )
    # Risk of capital must be <= 2%.
    assert out.proposed_risk_pct <= 2.0 + 1e-6
    assert out.reasoning.get("risk_cap_triggered") is True


def test_risk_cap_not_triggered_when_kelly_already_within_limit():
    # Lower prob + symmetric 1R payoff keeps quarter-Kelly below the 2%
    # risk cap.
    out = compute_proposal(
        inp=_default_input(
            loss_per_unit=0.05,
            stop_price=95.0,
            payoff_fraction=0.05,
            calibrated_prob=0.52,
            max_risk_pct=2.0,
        ),
        source="unit",
    )
    assert out.reasoning.get("risk_cap_triggered") is False


# ---------------------------------------------------------------------------
# Correlation + single-ticker caps
# ---------------------------------------------------------------------------


def test_correlation_cap_triggered_when_bucket_full():
    inp = _default_input()
    correlation = CorrelationBudget(
        bucket="equity:A",
        open_notional=14_500.0,         # close to the 15% of 100k cap
        max_bucket_notional=15_000.0,
    )
    out = compute_proposal(inp=inp, correlation=correlation, source="unit")
    assert out.correlation_cap_triggered is True
    # Proposed notional should equal the remaining bucket headroom.
    assert out.proposed_notional <= 500.0 + 1e-6


def test_correlation_cap_not_triggered_when_bucket_empty():
    inp = _default_input()
    correlation = CorrelationBudget(
        bucket="equity:A",
        open_notional=0.0,
        max_bucket_notional=15_000.0,
    )
    out = compute_proposal(inp=inp, correlation=correlation, source="unit")
    assert out.correlation_cap_triggered is False


def test_single_ticker_cap_triggered_when_already_held():
    inp = _default_input(single_ticker_cap_pct=5.0)  # 5% => $5k cap on a 100k book
    portfolio = PortfolioBudget(
        total_capital=100_000.0,
        deployed_notional=0.0,
        max_total_notional=100_000.0,
        ticker_open_notional=4_800.0,
    )
    out = compute_proposal(inp=inp, portfolio=portfolio, source="unit")
    # Remaining single-ticker headroom is $200, so proposal must not exceed it.
    assert out.notional_cap_triggered is True
    assert out.proposed_notional <= 200.0 + 1e-6


def test_portfolio_cap_trims_proposal():
    inp = _default_input()
    portfolio = PortfolioBudget(
        total_capital=100_000.0,
        deployed_notional=99_000.0,
        max_total_notional=99_500.0,
        ticker_open_notional=0.0,
    )
    out = compute_proposal(inp=inp, portfolio=portfolio, source="unit")
    assert out.notional_cap_triggered is True
    assert out.proposed_notional <= 500.0 + 1e-6


# ---------------------------------------------------------------------------
# Rounding
# ---------------------------------------------------------------------------


def test_int_rounding_produces_whole_shares():
    out = compute_proposal(inp=_default_input(qty_rounding="int"), source="unit")
    assert out.proposed_quantity == float(int(out.proposed_quantity))


def test_decimal_rounding_allows_fractional_crypto_qty():
    out = compute_proposal(
        inp=_default_input(
            ticker="BTC-USD",
            asset_class="crypto",
            entry_price=50_000.0,
            stop_price=48_000.0,
            payoff_fraction=0.08,
            loss_per_unit=0.04,
            calibrated_prob=0.60,
            qty_rounding="decimal",
        ),
        source="unit",
    )
    assert out.proposed_quantity > 0
    # The quantity should be fractional (50k/share means a 100k book cannot
    # achieve a whole share under a 2% risk cap).
    assert out.proposed_quantity < 1.0


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_determinism_same_input_same_output():
    inp = _default_input()
    a = compute_proposal(inp=inp, source="unit")
    b = compute_proposal(inp=inp, source="unit")
    assert a.proposed_notional == b.proposed_notional
    assert a.proposed_quantity == b.proposed_quantity
    assert a.kelly_fraction == b.kelly_fraction
    assert a.proposal_id == b.proposal_id


def test_proposal_id_is_stable_across_calls():
    a = compute_proposal_id(
        source="alerts", ticker="AAPL", user_id=7,
        entry_price=100.0, stop_price=95.0,
        calibrated_prob=0.6, payoff_fraction=0.1, loss_per_unit=0.05,
    )
    b = compute_proposal_id(
        source="alerts", ticker="AAPL", user_id=7,
        entry_price=100.0, stop_price=95.0,
        calibrated_prob=0.6, payoff_fraction=0.1, loss_per_unit=0.05,
    )
    assert a == b
    assert len(a) == 32


def test_proposal_id_changes_with_input():
    a = compute_proposal_id(
        source="alerts", ticker="AAPL", user_id=7,
        entry_price=100.0, stop_price=95.0,
        calibrated_prob=0.6, payoff_fraction=0.1, loss_per_unit=0.05,
    )
    b = compute_proposal_id(
        source="alerts", ticker="MSFT", user_id=7,
        entry_price=100.0, stop_price=95.0,
        calibrated_prob=0.6, payoff_fraction=0.1, loss_per_unit=0.05,
    )
    assert a != b


# ---------------------------------------------------------------------------
# Notional consistency
# ---------------------------------------------------------------------------


def test_notional_equals_quantity_times_entry_within_rounding():
    out = compute_proposal(inp=_default_input(), source="unit")
    assert out.proposed_notional == pytest.approx(
        out.proposed_quantity * 100.0, rel=1e-9, abs=1e-4,
    )


def test_risk_pct_matches_notional_times_loss_fraction():
    inp = _default_input()
    out = compute_proposal(inp=inp, source="unit")
    expected_risk = out.proposed_notional * inp.loss_per_unit / inp.capital * 100.0
    assert out.proposed_risk_pct == pytest.approx(expected_risk, rel=1e-6, abs=1e-6)
