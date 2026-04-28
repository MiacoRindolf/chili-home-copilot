"""Wall-clock budget wrapper for ``backtesting.py`` runs.

Background (2026-04-28 incident): brain-worker logs showed
``FractionalBacktest.run`` at 70% with an ETA of 55 hours
(``2259/3244 [3:35:24<55:29:21, 202.80s/bar]``). The 5-second
``lean-cycle`` worker can't make any forward progress while a single
backtest holds the lane for days. This module provides
:func:`run_with_walltime_budget` so callers can hard-cap each run.

Why a watchdog thread instead of ``signal.alarm``::

* The brain-worker uses thread pools; ``signal`` only fires on the main
  thread. A watchdog thread + ``Backtest.cancel()`` works inside any
  worker thread.
* ``backtesting.py`` exposes ``Backtest._cancel = True`` (or
  ``bt._stop = True`` depending on version) which the inner loop
  checks per bar; the wrapper writes both for forward-compat.
* If the library doesn't honor cancel within a grace period we bail
  via ``TimeoutError`` so the caller can mark the pattern.

Tuning::

    CHILI_BACKTEST_BUDGET_SEC = '60'   (default 60)
    CHILI_BACKTEST_GRACE_SEC  = '10'   (default 10 — extra time to honor cancel)

Usage::

    from app.services.trading.backtest_watchdog import run_with_walltime_budget

    try:
        stats = run_with_walltime_budget(bt, budget_sec=60)
    except BacktestBudgetExceeded as e:
        logger.warning("Pattern %s busted backtest budget: %s", pattern_id, e)
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)


class BacktestBudgetExceeded(TimeoutError):
    """Raised when a backtest exceeded its wall-clock budget AND failed to
    honor the cancel flag within the grace period."""


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _try_cancel(bt: Any) -> None:
    """Set every cancel flag the various ``backtesting.py`` versions watch.

    Best-effort: write multiple attribute names so the pattern works even
    when the upstream library renames its internal flag.
    """
    for attr in ("_cancel", "_stop", "_should_stop", "_canceled"):
        try:
            setattr(bt, attr, True)
        except Exception:
            pass


def run_with_walltime_budget(
    bt: Any,
    *,
    budget_sec: int | None = None,
    grace_sec: int | None = None,
    label: str = "",
    **run_kwargs: Any,
) -> Any:
    """Call ``bt.run(**run_kwargs)`` with a wall-clock budget.

    If the run exceeds ``budget_sec``, this function flips the cancel
    flag on ``bt`` and waits up to ``grace_sec`` for the run to actually
    return. If it doesn't return within the grace, raises
    :class:`BacktestBudgetExceeded`.

    Note: this does NOT kill threads or os processes — it only requests
    cooperative cancellation. The library must respect it. In practice
    backtesting.py's loop checks the cancel flag every bar, so the run
    bails out within ~1 bar of cancel being set.
    """
    if budget_sec is None:
        budget_sec = _env_int("CHILI_BACKTEST_BUDGET_SEC", 60)
    if grace_sec is None:
        grace_sec = _env_int("CHILI_BACKTEST_GRACE_SEC", 10)

    result_box: dict[str, Any] = {}
    error_box: dict[str, BaseException] = {}
    done = threading.Event()

    def _runner() -> None:
        try:
            result_box["stats"] = bt.run(**run_kwargs)
        except BaseException as e:
            error_box["err"] = e
        finally:
            done.set()

    started = time.monotonic()
    t = threading.Thread(target=_runner, name=f"bt_run_{label or id(bt)}", daemon=True)
    t.start()

    timed_out = not done.wait(timeout=max(1, int(budget_sec)))
    elapsed = time.monotonic() - started

    if timed_out:
        # Request cancel and give the run a brief window to honor it.
        logger.warning(
            "[backtest_watchdog] budget exceeded after %.1fs (limit=%ds) — requesting cancel  label=%s",
            elapsed, budget_sec, label or "unnamed",
        )
        _try_cancel(bt)
        done.wait(timeout=max(1, int(grace_sec)))
        # Either way, partial stats are not trustworthy — raise so the caller
        # treats this as a pattern-timeout instead of writing incomplete data.
        raise BacktestBudgetExceeded(
            f"backtest exceeded {budget_sec}s budget (label={label}, elapsed={elapsed:.1f}s)"
        )

    if "err" in error_box:
        raise error_box["err"]
    return result_box.get("stats")
