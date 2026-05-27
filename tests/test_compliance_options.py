from __future__ import annotations

from types import SimpleNamespace

import pytest


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._rows)


class _FakeDb:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *_args, **_kwargs):
        return _FakeQuery(self._rows)


def test_compliance_concentration_counts_option_contract_notional() -> None:
    from app.services.trading.compliance import check_concentration_limits

    option_trade = SimpleNamespace(
        ticker="SPY",
        entry_price=1.25,
        quantity=3,
        asset_kind="option",
        indicator_snapshot={"option_meta": {"strike": 729.0}},
    )

    ok, reason = check_concentration_limits(
        _FakeDb([option_trade]),
        user_id=None,
        ticker="SPY",
        proposed_notional=1_000.0,
        total_equity=5_000.0,
    )

    assert not ok
    assert reason is not None
    assert "27.5% of equity" in reason


def test_compliance_trade_notional_keeps_equity_share_math() -> None:
    from app.services.trading.compliance import _trade_notional_usd

    stock_trade = SimpleNamespace(
        ticker="SPY",
        entry_price=100.0,
        quantity=2,
        indicator_snapshot={},
    )

    assert _trade_notional_usd(stock_trade) == pytest.approx(200.0)
