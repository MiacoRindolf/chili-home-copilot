from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services.trading.options import portfolio_budget as budget_mod
from app.services.trading.options.portfolio_budget import check_proposal_against_budget
from app.services.trading.options.strategies import (
    cash_secured_put,
    covered_call,
    iron_condor,
    persist_proposal,
    vertical_spread,
)


def _assert_missing_risk(proposal, expected_field: str) -> None:
    assert proposal.net_delta is None
    assert proposal.net_gamma is None
    assert proposal.net_theta is None
    assert proposal.net_vega is None
    assert proposal.meta["risk_status"] == "missing_greek_inputs"
    assert expected_field in proposal.meta["missing_greek_inputs"]


def test_valid_covered_call_keeps_finite_strategy_greeks():
    proposal = covered_call(
        underlying="SPY",
        spot=500.0,
        short_call_strike=515.0,
        short_call_expiration=date.today() + timedelta(days=30),
        short_call_premium=4.25,
    )

    assert proposal.meta == {}
    assert proposal.net_delta is not None
    assert proposal.net_gamma is not None
    assert proposal.net_theta is not None
    assert proposal.net_vega is not None


def test_covered_call_invalid_spot_marks_missing_greek_risk():
    proposal = covered_call(
        underlying="SPY",
        spot=0.0,
        short_call_strike=515.0,
        short_call_expiration=date.today() + timedelta(days=30),
        short_call_premium=4.25,
    )

    _assert_missing_risk(proposal, "spot")
    assert proposal.max_loss is None
    assert proposal.max_profit is None
    assert proposal.breakevens == []


def test_covered_call_malformed_expiration_marks_missing_greek_risk():
    proposal = covered_call(
        underlying="SPY",
        spot=500.0,
        short_call_strike=515.0,
        short_call_expiration="not-a-date",  # type: ignore[arg-type]
        short_call_premium=4.25,
    )

    _assert_missing_risk(proposal, "leg_0:expiration")
    assert proposal.legs[0].occ_symbol == "SPY000000C00515000"


def test_cash_secured_put_zero_premium_marks_missing_greek_risk():
    proposal = cash_secured_put(
        underlying="SPY",
        spot=500.0,
        short_put_strike=485.0,
        short_put_expiration=date.today() + timedelta(days=30),
        short_put_premium=0.0,
    )

    _assert_missing_risk(proposal, "leg_0:entry_price")
    assert proposal.net_credit is None
    assert proposal.max_loss is None


def test_cash_secured_put_impossible_premium_marks_missing_risk():
    proposal = cash_secured_put(
        underlying="SPY",
        spot=500.0,
        short_put_strike=485.0,
        short_put_expiration=date.today() + timedelta(days=30),
        short_put_premium=500.0,
    )

    _assert_missing_risk(proposal, "cash_secured_put:premium_exceeds_strike")
    assert proposal.net_credit is None
    assert proposal.max_loss is None


def test_bull_call_spread_nan_premium_marks_missing_greek_risk():
    proposal = vertical_spread(
        underlying="SPY",
        spot=500.0,
        long_strike=500.0,
        short_strike=515.0,
        expiration=date.today() + timedelta(days=30),
        long_premium=float("nan"),
        short_premium=3.10,
        direction="bull_call",
    )

    _assert_missing_risk(proposal, "leg_0:entry_price")
    assert proposal.net_debit is None
    assert proposal.max_profit is None


def test_bull_call_spread_credit_quote_marks_missing_risk():
    proposal = vertical_spread(
        underlying="SPY",
        spot=500.0,
        long_strike=500.0,
        short_strike=515.0,
        expiration=date.today() + timedelta(days=30),
        long_premium=2.00,
        short_premium=3.10,
        direction="bull_call",
    )

    _assert_missing_risk(proposal, "vertical_spread:net_debit_nonpositive")
    assert proposal.net_debit is None
    assert proposal.max_loss is None


def test_bull_call_spread_debit_above_width_marks_missing_risk():
    proposal = vertical_spread(
        underlying="SPY",
        spot=500.0,
        long_strike=500.0,
        short_strike=505.0,
        expiration=date.today() + timedelta(days=30),
        long_premium=10.00,
        short_premium=1.00,
        direction="bull_call",
    )

    _assert_missing_risk(proposal, "vertical_spread:net_debit_exceeds_width")
    assert proposal.net_debit is None
    assert proposal.max_profit is None


def test_bull_call_spread_nan_strike_marks_missing_greek_risk():
    proposal = vertical_spread(
        underlying="SPY",
        spot=500.0,
        long_strike=float("nan"),
        short_strike=515.0,
        expiration=date.today() + timedelta(days=30),
        long_premium=7.50,
        short_premium=3.10,
        direction="bull_call",
    )

    _assert_missing_risk(proposal, "leg_0:strike")
    assert proposal.net_debit is None
    assert proposal.max_loss is None


def test_iron_condor_expired_contract_marks_missing_greek_risk():
    proposal = iron_condor(
        underlying="SPY",
        spot=500.0,
        short_put_strike=485.0,
        long_put_strike=480.0,
        short_call_strike=515.0,
        long_call_strike=520.0,
        expiration=date.today() - timedelta(days=1),
        short_put_premium=3.20,
        long_put_premium=1.15,
        short_call_premium=3.10,
        long_call_premium=1.05,
    )

    _assert_missing_risk(proposal, "leg_0:expiration_expired")


def test_iron_condor_impossible_credit_marks_missing_risk():
    proposal = iron_condor(
        underlying="SPY",
        spot=500.0,
        short_put_strike=485.0,
        long_put_strike=480.0,
        short_call_strike=515.0,
        long_call_strike=520.0,
        expiration=date.today() + timedelta(days=30),
        short_put_premium=10.00,
        long_put_premium=0.10,
        short_call_premium=10.00,
        long_call_premium=0.10,
    )

    _assert_missing_risk(proposal, "iron_condor:net_credit_exceeds_wing")
    assert proposal.net_credit is None
    assert proposal.max_loss is None


def test_budget_blocks_strategy_proposal_with_missing_greek_risk(monkeypatch):
    monkeypatch.delenv("CHILI_OPTIONS_BUDGET_BYPASS", raising=False)
    monkeypatch.setattr(
        budget_mod,
        "_get_budget",
        lambda *_args, **_kwargs: {
            "max_abs_delta": 100.0,
            "max_abs_gamma": 100.0,
            "max_total_vega": 100.0,
            "max_theta_burn_per_day": 100.0,
        },
    )
    monkeypatch.setattr(
        budget_mod,
        "_sum_open_position_greeks",
        lambda *_args, **_kwargs: {
            "net_delta": 0.0,
            "net_gamma": 0.0,
            "net_theta": 0.0,
            "net_vega": 0.0,
            "missing_greeks_count": 0,
        },
    )
    proposal = covered_call(
        underlying="SPY",
        spot=0.0,
        short_call_strike=515.0,
        short_call_expiration=date.today() + timedelta(days=30),
        short_call_premium=4.25,
    )

    result = check_proposal_against_budget(None, 1, proposal)

    assert result.accepted is False
    assert result.reasons == ["missing_complete_greeks:delta,gamma,theta,vega"]


def test_budget_blocks_strategy_proposal_with_impossible_payoff_geometry(monkeypatch):
    monkeypatch.delenv("CHILI_OPTIONS_BUDGET_BYPASS", raising=False)
    monkeypatch.setattr(
        budget_mod,
        "_get_budget",
        lambda *_args, **_kwargs: {
            "max_abs_delta": 100.0,
            "max_abs_gamma": 100.0,
            "max_total_vega": 100.0,
            "max_theta_burn_per_day": 100.0,
        },
    )
    monkeypatch.setattr(
        budget_mod,
        "_sum_open_position_greeks",
        lambda *_args, **_kwargs: {
            "net_delta": 0.0,
            "net_gamma": 0.0,
            "net_theta": 0.0,
            "net_vega": 0.0,
            "missing_greeks_count": 0,
        },
    )
    proposal = vertical_spread(
        underlying="SPY",
        spot=500.0,
        long_strike=500.0,
        short_strike=515.0,
        expiration=date.today() + timedelta(days=30),
        long_premium=2.00,
        short_premium=3.10,
        direction="bull_call",
    )

    result = check_proposal_against_budget(None, 1, proposal)

    assert result.accepted is False
    assert result.reasons == ["missing_complete_greeks:delta,gamma,theta,vega"]


def test_persist_proposal_refuses_missing_greek_risk_before_db_call():
    class _Db:
        executed = False

        def execute(self, *_args, **_kwargs):
            self.executed = True
            raise AssertionError("missing-risk proposal should not be inserted")

    db = _Db()
    proposal = covered_call(
        underlying="SPY",
        spot=0.0,
        short_call_strike=515.0,
        short_call_expiration=date.today() + timedelta(days=30),
        short_call_premium=4.25,
    )

    assert persist_proposal(db, user_id=1, proposal=proposal) is None
    assert db.executed is False


def test_persist_proposal_refuses_impossible_payoff_geometry_before_db_call():
    class _Db:
        executed = False

        def execute(self, *_args, **_kwargs):
            self.executed = True
            raise AssertionError("missing-risk proposal should not be inserted")

    db = _Db()
    proposal = iron_condor(
        underlying="SPY",
        spot=500.0,
        short_put_strike=485.0,
        long_put_strike=480.0,
        short_call_strike=515.0,
        long_call_strike=520.0,
        expiration=date.today() + timedelta(days=30),
        short_put_premium=10.00,
        long_put_premium=0.10,
        short_call_premium=10.00,
        long_call_premium=0.10,
    )

    assert persist_proposal(db, user_id=1, proposal=proposal) is None
    assert db.executed is False
