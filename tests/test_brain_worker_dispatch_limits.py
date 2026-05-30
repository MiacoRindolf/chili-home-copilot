from __future__ import annotations

from scripts.brain_worker import _brain_work_dispatch_kwargs_for_mode


def test_backtest_mode_skips_mining_work_dispatch() -> None:
    kwargs = _brain_work_dispatch_kwargs_for_mode("backtest")

    assert kwargs["max_mine"] == 0
    assert kwargs["run_market_snapshots_watchdog"] is False


def test_default_mode_keeps_full_work_dispatch() -> None:
    assert _brain_work_dispatch_kwargs_for_mode("lean-cycle") == {}
