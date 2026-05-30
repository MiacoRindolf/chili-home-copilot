from __future__ import annotations

from datetime import date

import pytest

from app.services.trading.options import portfolio_budget as mod
from app.services.trading.options.portfolio_budget import (
    check_proposal_against_budget,
    single_leg_proposal_from_option_meta,
)
from app.services.trading.options.strategies import Leg, StrategyProposal


class _Result:
    def __init__(self, rows=None, row=None):
        self._rows = rows or []
        self._row = row

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._row


class _Db:
    def execute(self, stmt, params=None):
        sql = str(stmt)
        if "FROM options_position" in sql:
            return _Result(
                rows=[
                    (
                        [
                            {
                                "qty": 1,
                                "delta": 0.10,
                                "gamma": 0.01,
                                "theta": -0.02,
                                "vega": 0.03,
                            }
                        ],
                    )
                ]
            )
        if "FROM trading_trades" in sql:
            return _Result(
                rows=[
                    (
                        2,
                        {
                            "option_meta": {
                                "delta": 0.20,
                                "gamma": 0.02,
                                "theta": -0.03,
                                "vega": 0.04,
                            }
                        },
                    )
                ]
            )
        if "FROM options_greeks_budget" in sql:
            return _Result(row=None)
        raise AssertionError(f"unexpected SQL: {sql}")


class _MalformedTradeQuantityDb:
    def execute(self, stmt, params=None):
        sql = str(stmt)
        if "FROM options_position" in sql:
            return _Result(rows=[])
        if "FROM trading_trades" in sql:
            return _Result(
                rows=[
                    (
                        "1.5",
                        {
                            "option_meta": {
                                "delta": 0.20,
                                "gamma": 0.02,
                                "theta": -0.03,
                                "vega": 0.04,
                            }
                        },
                    )
                ]
            )
        raise AssertionError(f"unexpected SQL: {sql}")


class _OpenTradeGreeksUnavailableDb:
    def execute(self, stmt, params=None):
        sql = str(stmt)
        if "FROM options_position" in sql:
            return _Result(rows=[])
        if "FROM trading_trades" in sql:
            raise RuntimeError("trade projection unavailable")
        if "FROM options_greeks_budget" in sql:
            return _Result(row=None)
        raise AssertionError(f"unexpected SQL: {sql}")


def _proposal(**overrides) -> StrategyProposal:
    base = dict(
        underlying="SPY",
        strategy_family="single_long_option",
        legs=[
            Leg(
                occ_symbol="SPY260619C00729000",
                underlying="SPY",
                expiration=date(2026, 6, 19),
                strike=729.0,
                opt_type="call",
                qty=1,
                entry_price=4.01,
            )
        ],
        net_debit=401.0,
        net_credit=None,
        max_loss=401.0,
        max_profit=None,
        breakevens=[],
        net_delta=0.10,
        net_gamma=0.01,
        net_theta=-0.02,
        net_vega=0.03,
        confidence=0.5,
        rationale="test",
    )
    base.update(overrides)
    return StrategyProposal(**base)


def test_sum_open_position_greeks_adds_positions_and_open_trade_snapshots():
    totals = mod._sum_open_position_greeks(_Db(), user_id=1)

    assert totals["net_delta"] == pytest.approx(0.50)
    assert totals["net_gamma"] == pytest.approx(0.05)
    assert totals["net_theta"] == pytest.approx(-0.08)
    assert totals["net_vega"] == pytest.approx(0.11)
    assert totals["missing_greeks_count"] == 0


def test_sum_open_trade_greeks_marks_malformed_open_quantity_as_unproven():
    totals = mod._sum_open_position_greeks(_MalformedTradeQuantityDb(), user_id=1)

    assert totals["net_delta"] == 0.0
    assert totals["missing_greeks_count"] == 1


def test_sum_open_trade_greeks_marks_fetch_failure_as_unproven():
    totals = mod._sum_open_position_greeks(_OpenTradeGreeksUnavailableDb(), user_id=1)

    assert totals["net_delta"] == 0.0
    assert totals["missing_greeks_count"] == 1


def test_check_proposal_blocks_when_open_trade_greeks_are_unavailable(monkeypatch):
    monkeypatch.delenv("CHILI_OPTIONS_BUDGET_BYPASS", raising=False)

    result = check_proposal_against_budget(
        _OpenTradeGreeksUnavailableDb(),
        1,
        _proposal(),
    )

    assert result.accepted is False
    assert result.reasons == ["missing_complete_greeks:open_positions:1"]


def test_single_leg_proposal_requires_complete_finite_greeks():
    with pytest.raises(ValueError, match="missing_greeks:vega"):
        single_leg_proposal_from_option_meta(
            {
                "underlying": "SPY",
                "expiration": "2026-06-19",
                "strike": 729.0,
                "option_type": "call",
                "limit_price": 4.01,
                "quantity": 1,
                "delta": 0.42,
                "gamma": 0.03,
                "theta": -0.08,
            }
        )


def test_single_leg_proposal_rejects_malformed_contract_quantity():
    with pytest.raises(ValueError, match="invalid_quantity"):
        single_leg_proposal_from_option_meta(
            {
                "underlying": "SPY",
                "expiration": "2026-06-19",
                "strike": 729.0,
                "option_type": "call",
                "limit_price": 4.01,
                "quantity": "1.5",
                "delta": 0.42,
                "gamma": 0.03,
                "theta": -0.08,
                "vega": 0.11,
            }
        )


def test_check_proposal_against_budget_blocks_missing_complete_greeks(monkeypatch):
    monkeypatch.delenv("CHILI_OPTIONS_BUDGET_BYPASS", raising=False)
    monkeypatch.setattr(
        mod,
        "_get_budget",
        lambda *_args, **_kwargs: {
            "max_abs_delta": 100.0,
            "max_abs_gamma": 100.0,
            "max_total_vega": 100.0,
            "max_theta_burn_per_day": 100.0,
        },
    )
    monkeypatch.setattr(
        mod,
        "_sum_open_position_greeks",
        lambda *_args, **_kwargs: {
            "net_delta": 0.0,
            "net_gamma": 0.0,
            "net_theta": 0.0,
            "net_vega": 0.0,
            "missing_greeks_count": 0,
        },
    )

    result = check_proposal_against_budget(None, 1, _proposal(net_gamma=None))

    assert result.accepted is False
    assert result.reasons == ["missing_complete_greeks:gamma"]


def test_check_proposal_against_budget_rejects_boolean_proposal_greeks(monkeypatch):
    monkeypatch.delenv("CHILI_OPTIONS_BUDGET_BYPASS", raising=False)
    monkeypatch.setattr(
        mod,
        "_get_budget",
        lambda *_args, **_kwargs: {
            "max_abs_delta": 100.0,
            "max_abs_gamma": 100.0,
            "max_total_vega": 100.0,
            "max_theta_burn_per_day": 100.0,
        },
    )
    monkeypatch.setattr(
        mod,
        "_sum_open_position_greeks",
        lambda *_args, **_kwargs: {
            "net_delta": 0.0,
            "net_gamma": 0.0,
            "net_theta": 0.0,
            "net_vega": 0.0,
            "missing_greeks_count": 0,
        },
    )

    result = check_proposal_against_budget(
        None,
        1,
        _proposal(
            net_delta=True,
            net_gamma=True,
            net_theta=True,
            net_vega=True,
        ),
    )

    assert result.accepted is False
    assert result.reasons == ["missing_complete_greeks:delta,gamma,theta,vega"]
    assert result.after_proposal == {
        "net_delta": 0.0,
        "net_gamma": 0.0,
        "net_theta": 0.0,
        "net_vega": 0.0,
    }


def test_check_proposal_against_budget_blocks_unproven_open_book(monkeypatch):
    monkeypatch.delenv("CHILI_OPTIONS_BUDGET_BYPASS", raising=False)
    monkeypatch.setattr(
        mod,
        "_get_budget",
        lambda *_args, **_kwargs: {
            "max_abs_delta": 100.0,
            "max_abs_gamma": 100.0,
            "max_total_vega": 100.0,
            "max_theta_burn_per_day": 100.0,
        },
    )
    monkeypatch.setattr(
        mod,
        "_sum_open_position_greeks",
        lambda *_args, **_kwargs: {
            "net_delta": 0.0,
            "net_gamma": 0.0,
            "net_theta": 0.0,
            "net_vega": 0.0,
            "missing_greeks_count": 2,
        },
    )

    result = check_proposal_against_budget(None, 1, _proposal())

    assert result.accepted is False
    assert result.reasons == ["missing_complete_greeks:open_positions:2"]


def test_check_proposal_against_budget_fails_closed_on_book_error(monkeypatch):
    monkeypatch.delenv("CHILI_OPTIONS_BUDGET_BYPASS", raising=False)

    def _raise(*_args, **_kwargs):
        raise RuntimeError("open book unavailable")

    monkeypatch.setattr(
        mod,
        "_get_budget",
        lambda *_args, **_kwargs: {
            "max_abs_delta": 100.0,
            "max_abs_gamma": 100.0,
            "max_total_vega": 100.0,
            "max_theta_burn_per_day": 100.0,
        },
    )
    monkeypatch.setattr(mod, "_sum_open_position_greeks", _raise)

    result = check_proposal_against_budget(None, 1, _proposal())

    assert result.accepted is False
    assert result.reasons == ["budget_error:RuntimeError"]
    assert result.current_portfolio["missing_greeks_count"] == 0
    assert result.after_proposal["net_delta"] == 0.0


def test_check_proposal_against_budget_bypass_audits_book_error(monkeypatch):
    monkeypatch.setenv("CHILI_OPTIONS_BUDGET_BYPASS", "true")

    def _raise(*_args, **_kwargs):
        raise RuntimeError("open book unavailable")

    monkeypatch.setattr(mod, "_sum_open_position_greeks", _raise)

    result = check_proposal_against_budget(None, 1, _proposal())

    assert result.accepted is True
    assert result.reasons == [
        "BYPASS_VIA_CHILI_OPTIONS_BUDGET_BYPASS",
        "budget_error:RuntimeError",
    ]


def test_explicit_options_budget_bypass_is_auditable(monkeypatch):
    monkeypatch.setenv("CHILI_OPTIONS_BUDGET_BYPASS", "true")
    monkeypatch.setattr(
        mod,
        "_get_budget",
        lambda *_args, **_kwargs: {
            "max_abs_delta": 100.0,
            "max_abs_gamma": 100.0,
            "max_total_vega": 100.0,
            "max_theta_burn_per_day": 100.0,
        },
    )
    monkeypatch.setattr(
        mod,
        "_sum_open_position_greeks",
        lambda *_args, **_kwargs: {
            "net_delta": 0.0,
            "net_gamma": 0.0,
            "net_theta": 0.0,
            "net_vega": 0.0,
            "missing_greeks_count": 0,
        },
    )

    result = check_proposal_against_budget(None, 1, _proposal(net_gamma=None))

    assert result.accepted is True
    assert result.reasons == [
        "BYPASS_VIA_CHILI_OPTIONS_BUDGET_BYPASS",
        "missing_complete_greeks:gamma",
    ]
