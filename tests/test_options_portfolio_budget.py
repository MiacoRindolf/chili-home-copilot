from __future__ import annotations

import json
from datetime import date, timedelta

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


class _BadBudgetDb:
    def execute(self, stmt, params=None):
        sql = str(stmt)
        if "FROM options_greeks_budget" in sql:
            return _Result(
                row=(
                    float("nan"),
                    True,
                    {"30d": float("inf"), "60d": 70.0, "bad": -1.0},
                    float("inf"),
                    True,
                )
            )
        raise AssertionError(f"unexpected SQL: {sql}")


class _BooleanPositionQtyDb:
    def execute(self, stmt, params=None):
        sql = str(stmt)
        if "FROM options_position" in sql:
            return _Result(
                rows=[
                    (
                        [
                            {
                                "qty": True,
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
            return _Result(rows=[])
        raise AssertionError(f"unexpected SQL: {sql}")


class _ImplausibleOpenGreeksDb:
    def execute(self, stmt, params=None):
        sql = str(stmt)
        if "FROM options_position" in sql:
            return _Result(
                rows=[
                    (
                        [
                            {
                                "qty": 1,
                                "expiration": date.today() + timedelta(days=20),
                                "delta": 4.20,
                                "gamma": -0.01,
                                "theta": -0.02,
                                "vega": -0.03,
                            }
                        ],
                    )
                ]
            )
        if "FROM trading_trades" in sql:
            return _Result(
                rows=[
                    (
                        1,
                        {
                            "option_meta": {
                                "delta": 0.20,
                                "gamma": -0.02,
                                "theta": -0.03,
                                "vega": 0.04,
                            }
                        },
                    )
                ]
            )
        raise AssertionError(f"unexpected SQL: {sql}")


class _OpenGreeksSnapshotFallbackDb:
    def execute(self, stmt, params=None):
        sql = str(stmt)
        if "FROM options_position" in sql:
            return _Result(
                rows=[
                    (
                        [
                            {
                                "qty": 1,
                                "expiration": date.today() + timedelta(days=20),
                                "delta": 4.20,
                                "gamma": -0.01,
                                "theta": -0.02,
                                "vega": -0.03,
                                "quote_snapshot": {
                                    "delta": 0.10,
                                    "gamma": 0.01,
                                    "vega": 0.03,
                                },
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
                                "expiration": (
                                    date.today() + timedelta(days=20)
                                ).isoformat(),
                                "delta": 5.0,
                                "gamma": -0.02,
                                "theta": -0.03,
                                "vega": 0.04,
                                "quote_snapshot": {
                                    "delta": 0.20,
                                    "gamma": 0.02,
                                },
                            }
                        },
                    )
                ]
            )
        raise AssertionError(f"unexpected SQL: {sql}")


class _CaptureTradeGreeksSqlDb:
    def __init__(self):
        self.sql = ""
        self.params = None

    def execute(self, stmt, params=None):
        self.sql = str(stmt)
        self.params = dict(params or {})
        return _Result(rows=[])


class _CaptureBudgetUpsertDb:
    def __init__(self):
        self.params = None
        self.committed = False

    def execute(self, _stmt, params=None):
        self.params = dict(params or {})

    def commit(self):
        self.committed = True

    def rollback(self):
        raise AssertionError("rollback should not be called")


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


def test_sum_open_trade_greeks_filters_option_contract_aliases():
    db = _CaptureTradeGreeksSqlDb()

    totals = mod._sum_open_trade_greeks(db, user_id=7)

    assert totals["missing_greeks_count"] == 0
    assert db.params == {"uid": 7}
    sql = " ".join(db.sql.split())
    assert "REPLACE(LOWER(COALESCE(asset_kind, '')), '-', '_')" in sql
    assert "indicator_snapshot::jsonb ->> 'asset_kind'" in sql
    assert "(indicator_snapshot::jsonb -> 'breakout_alert') ->> 'asset_type'" in sql
    assert "'option_contracts'" in sql
    assert "'contract_options'" in sql


def test_get_budget_sanitizes_bad_persisted_limits():
    budget = mod._get_budget(_BadBudgetDb(), user_id=1)

    assert budget["max_abs_delta"] == 0.50
    assert budget["max_abs_gamma"] == 0.05
    assert budget["max_total_vega"] == 200.0
    assert budget["max_theta_burn_per_day"] == 50.0
    assert budget["max_vega_per_tenor"] == {"30d": 100, "60d": 70.0, "90d": 60}


def test_sum_open_position_greeks_marks_boolean_leg_qty_as_unproven():
    totals = mod._sum_open_position_greeks(_BooleanPositionQtyDb(), user_id=1)

    assert totals["net_delta"] == 0.0
    assert totals["net_gamma"] == 0.0
    assert totals["missing_greeks_count"] == 1


def test_sum_open_position_greeks_marks_implausible_leg_greeks_as_unproven():
    totals = mod._sum_open_position_greeks(_ImplausibleOpenGreeksDb(), user_id=1)

    assert totals["net_delta"] == pytest.approx(0.20)
    assert totals["net_gamma"] == 0.0
    assert totals["net_theta"] == pytest.approx(-0.05)
    assert totals["net_vega"] == pytest.approx(0.04)
    assert totals["missing_greeks_count"] == 2


def test_sum_open_position_greeks_uses_valid_snapshot_when_stale_top_level_bad():
    totals = mod._sum_open_position_greeks(_OpenGreeksSnapshotFallbackDb(), user_id=1)

    assert totals["net_delta"] == pytest.approx(0.50)
    assert totals["net_gamma"] == pytest.approx(0.05)
    assert totals["net_theta"] == pytest.approx(-0.08)
    assert totals["net_vega"] == pytest.approx(0.11)
    assert totals["vega_by_tenor"]["30d"] == pytest.approx(0.11)
    assert totals["missing_greeks_count"] == 0


def test_upsert_budget_sanitizes_bad_limits_before_persisting():
    db = _CaptureBudgetUpsertDb()

    assert mod.upsert_budget(
        db,
        1,
        max_abs_delta=float("nan"),
        max_abs_gamma=True,
        max_vega_per_tenor={"30d": float("inf"), "45d": 44.0},
        max_total_vega=float("inf"),
        max_theta_burn_per_day=True,
    ) is True

    assert db.committed is True
    assert db.params["d"] == 0.50
    assert db.params["g"] == 0.05
    assert db.params["tv"] == 200.0
    assert db.params["tb"] == 50.0
    assert json.loads(db.params["vt"]) == {"30d": 100, "60d": 80, "90d": 60, "45d": 44.0}


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


def test_single_leg_proposal_rejects_implausible_per_contract_greeks():
    with pytest.raises(ValueError, match="missing_greeks:delta,gamma,vega"):
        single_leg_proposal_from_option_meta(
            {
                "underlying": "SPY",
                "expiration": "2026-06-19",
                "strike": 729.0,
                "option_type": "call",
                "limit_price": 4.01,
                "quantity": 1,
                "delta": 4.20,
                "gamma": -0.03,
                "theta": -0.08,
                "vega": -0.11,
            }
        )


def test_single_leg_proposal_uses_snapshot_greeks_when_top_level_is_stale_bad():
    proposal = single_leg_proposal_from_option_meta(
        {
            "underlying": "SPY",
            "expiration": "2026-06-19",
            "strike": 729.0,
            "option_type": "call",
            "limit_price": 4.01,
            "quantity": 2,
            "delta": 4.20,
            "gamma": -0.03,
            "theta": -0.08,
            "vega": -0.11,
            "quote_snapshot": {
                "delta": 0.42,
                "gamma": 0.03,
                "vega": 0.11,
            },
        }
    )

    assert proposal.net_delta == pytest.approx(0.84)
    assert proposal.net_gamma == pytest.approx(0.06)
    assert proposal.net_theta == pytest.approx(-0.16)
    assert proposal.net_vega == pytest.approx(0.22)


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
        "vega_by_tenor": {},
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


def test_check_proposal_against_budget_enforces_tenor_vega_limit(monkeypatch):
    monkeypatch.delenv("CHILI_OPTIONS_BUDGET_BYPASS", raising=False)
    monkeypatch.setattr(
        mod,
        "_get_budget",
        lambda *_args, **_kwargs: {
            "max_abs_delta": 100.0,
            "max_abs_gamma": 100.0,
            "max_vega_per_tenor": {"30d": 0.05, "60d": 100.0, "90d": 100.0},
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
            "net_vega": 0.03,
            "vega_by_tenor": {"30d": 0.03},
            "missing_greeks_count": 0,
        },
    )

    proposal = _proposal(
        net_vega=0.03,
        legs=[
            Leg(
                occ_symbol="SPY_FUTURE_CALL",
                underlying="SPY",
                expiration=date.today() + timedelta(days=20),
                strike=729.0,
                opt_type="call",
                qty=1,
                entry_price=4.01,
            )
        ],
    )
    result = check_proposal_against_budget(None, 1, proposal)

    assert result.accepted is False
    assert result.after_proposal["net_vega"] == pytest.approx(0.06)
    assert result.after_proposal["vega_by_tenor"]["30d"] == pytest.approx(0.06)
    assert result.reasons == ["tenor_vega_breach:30d: |0.0600| > 0.05"]


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
