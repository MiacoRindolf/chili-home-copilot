from __future__ import annotations

from scripts.brain_worker import _brain_work_dispatch_kwargs_for_mode
from app.services.trading.brain_work.dispatcher import _dispatch_limits


def test_backtest_mode_skips_mining_work_dispatch() -> None:
    kwargs = _brain_work_dispatch_kwargs_for_mode("backtest")

    assert kwargs["max_mine"] == 0
    assert kwargs["run_market_snapshots_watchdog"] is False


def test_default_mode_keeps_full_work_dispatch() -> None:
    assert _brain_work_dispatch_kwargs_for_mode("lean-cycle") == {}


def test_dispatch_limits_claim_one_backtest_request_per_round() -> None:
    limits = dict(_dispatch_limits(max_backtest=8))

    assert limits["backtest_requested"] == 1


def test_dispatch_limits_preserve_backtest_zero_disable() -> None:
    limits = dict(_dispatch_limits(max_backtest=0))

    assert limits["backtest_requested"] == 0
