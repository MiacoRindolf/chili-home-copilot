from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models.trading import Trade
from app.services.trading.execution_quality import (
    _extract_signal_price,
    compute_execution_stats,
    compute_implementation_shortfall,
)


class _FakeQuery:
    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *args):
        return self

    def all(self):
        return list(self._rows)


class _FakeDb:
    def __init__(self, rows):
        self._rows = list(rows)

    def query(self, model):
        assert model is Trade
        return _FakeQuery(self._rows)


def _trade(**overrides):
    base = dict(
        ticker="SPY",
        entry_price=101.0,
        indicator_snapshot={"signal_price": 100.0},
        tags=None,
        asset_kind="stock",
        asset_type=None,
        tca_reference_entry_price=None,
        tca_reference_domain=None,
        tca_entry_slippage_bps=None,
        tca_exit_slippage_bps=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_compute_execution_stats_skips_malformed_prices_and_option_domain_mismatch():
    rows = [
        _trade(ticker="AAPL", entry_price=101.0, indicator_snapshot={"signal_price": 100.0}),
        _trade(ticker="BAD", entry_price=True, indicator_snapshot={"signal_price": 100.0}),
        _trade(
            ticker="SPY",
            asset_kind="option",
            entry_price=1.25,
            indicator_snapshot={"asset_type": "options", "signal_price": 715.0},
        ),
        _trade(
            ticker="SPY",
            asset_kind="option",
            entry_price=1.30,
            tca_reference_entry_price=1.25,
            tca_reference_domain="option_premium",
            indicator_snapshot={"asset_type": "options"},
        ),
    ]

    out = compute_execution_stats(_FakeDb(rows), user_id=1)

    assert out["measurable"] == 2
    assert out["avg_slippage_pct"] == pytest.approx(2.5)
    assert out["p90_slippage_pct"] == pytest.approx(4.0)
    assert out["by_class"]["stock"]["trades"] == 1
    assert out["by_class"]["option"]["trades"] == 1


def test_extract_signal_price_requires_option_premium_reference_for_options():
    underlying_domain = _trade(
        asset_kind="option",
        entry_price=1.25,
        indicator_snapshot={"asset_type": "options", "signal_price": 715.0},
    )
    premium_domain = _trade(
        asset_kind="option",
        entry_price=1.25,
        indicator_snapshot={
            "asset_type": "options",
            "signal_price": 1.25,
            "price_domains": {"entry_price": "option_premium"},
        },
    )

    assert _extract_signal_price(underlying_domain) is None
    assert _extract_signal_price(premium_domain) == pytest.approx(1.25)


def test_implementation_shortfall_skips_malformed_tca_bps():
    rows = [
        _trade(
            ticker="AAPL",
            entry_price=101.0,
            indicator_snapshot={"signal_price": 100.0},
            tca_entry_slippage_bps=5.0,
            tca_exit_slippage_bps=6.0,
        ),
        _trade(
            ticker="BAD",
            entry_price=101.0,
            indicator_snapshot={"signal_price": 100.0},
            tca_entry_slippage_bps=True,
            tca_exit_slippage_bps=6.0,
        ),
        _trade(
            ticker="SPY",
            asset_kind="option",
            entry_price=1.25,
            indicator_snapshot={"asset_type": "options", "signal_price": 715.0},
            tca_entry_slippage_bps=5.0,
            tca_exit_slippage_bps=6.0,
        ),
    ]

    out = compute_implementation_shortfall(_FakeDb(rows), user_id=1)

    assert out["measurable"] == 1
    assert out["mean_delay_bps"] == pytest.approx(100.0)
    assert out["mean_spread_bps"] == pytest.approx(11.0)
    assert out["mean_total_is_bps"] == pytest.approx(111.0)
