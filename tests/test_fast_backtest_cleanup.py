"""Tests for f-leak-4 phase 3 (FractionalBacktest strategy-instance cleanup).

The strat_cls created via ``type()`` in ``_run_dynamic_pattern_slice``
attaches heavy pandas arrays (``_indicator_arrays``, ``_atr_array``,
``_swing_low_array``) plus the parity sink list. If the third-party
FractionalBacktest library retains a reference to bt -> strategy ->
class, those heavy attrs accumulate at ~9 backtests/sec.

The fix: ``_cleanup_strat_cls`` clears those four attrs on every exit
path (success, budget-exceeded, exception). These tests pin the
contract going forward.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_run_dynamic_pattern_slice_calls_cleanup_on_success_path():
    """The cleanup helper must be called after _drain_backtest_parity_sink."""
    src = (REPO / "app/services/backtest_service.py").read_text()
    # Find _run_dynamic_pattern_slice + check the success path wires cleanup.
    idx = src.find("def _run_dynamic_pattern_slice(")
    assert idx > 0
    end = src.find("\ndef ", idx + 1)
    body = src[idx:end]
    assert "_cleanup_strat_cls" in body, "cleanup helper missing from function"
    # Drain + cleanup must both appear; cleanup should follow drain.
    drain_pos = body.find("_drain_backtest_parity_sink(strat_cls, ticker)")
    cleanup_pos = body.find("_cleanup_strat_cls()", drain_pos)
    assert drain_pos > 0
    assert cleanup_pos > drain_pos, (
        "cleanup must run AFTER drain on the success path "
        "(drain reads _parity_sink; cleanup empties it)"
    )


def test_run_dynamic_pattern_slice_calls_cleanup_on_budget_exceeded_path():
    """Cleanup must also run when the budget watchdog aborts the bt.run."""
    src = (REPO / "app/services/backtest_service.py").read_text()
    idx = src.find("def _run_dynamic_pattern_slice(")
    end = src.find("\ndef ", idx + 1)
    body = src[idx:end]
    # The except _BTBudgetExceeded block must call cleanup before return.
    except_pos = body.find("except _BTBudgetExceeded")
    return_pos = body.find('return {"ok": False, "error": f"backtest_budget_exceeded:', except_pos)
    cleanup_pos = body.find("_cleanup_strat_cls()", except_pos)
    assert except_pos > 0
    assert return_pos > except_pos
    assert cleanup_pos > 0
    assert except_pos < cleanup_pos < return_pos, (
        "cleanup must run between except _BTBudgetExceeded and return"
    )


def test_cleanup_clears_all_four_heavy_attrs():
    """The four heavy class attrs are: _parity_sink, _indicator_arrays,
    _atr_array, _swing_low_array. Each must be reset by the cleanup
    helper. Source-text guard against accidental future shrinkage of
    the cleanup set."""
    src = (REPO / "app/services/backtest_service.py").read_text()
    # Find the cleanup helper definition body.
    idx = src.find("def _cleanup_strat_cls()")
    assert idx > 0
    # Look at the next ~600 chars (helper body is short).
    body = src[idx:idx + 600]
    for attr in (
        "_parity_sink",
        "_indicator_arrays",
        "_atr_array",
        "_swing_low_array",
    ):
        assert f"strat_cls.{attr} =" in body, (
            f"cleanup helper does not reset strat_cls.{attr} -- "
            f"the heavy data stays attached when the library retains the class"
        )


def test_cleanup_swallows_exceptions():
    """The cleanup helper must NOT raise into the return path. Pin the
    try/except guard so a future refactor can't accidentally remove it."""
    src = (REPO / "app/services/backtest_service.py").read_text()
    idx = src.find("def _cleanup_strat_cls()")
    body = src[idx:idx + 600]
    assert "try:" in body
    assert "except Exception:" in body
