"""The shutdown log line must name whoever retired the loop.

57 outages in 14 days went unexplained because `stop()` logged "[live_loop]
stopped" and nothing else -- no caller, no reason, at INFO. These tests pin the
attribution so the next death explains itself.
"""

import logging

from app.services.trading.momentum_neural import live_runner_loop as lrl
from app.services.trading.momentum_neural.live_runner_loop import _stop_caller


def test_names_the_calling_function_and_line():
    def a_supervisor_calling_stop():
        return _stop_caller()

    who = a_supervisor_calling_stop()
    assert "a_supervisor_calling_stop()" in who
    assert "test_live_loop_stop_attribution.py:" in who


def test_skips_the_loops_own_frames():
    """stop() is re-entrant and stop_live_runner_loop() is a forwarder.

    Reporting either would name the loop as its own killer -- exactly as
    uninformative as the bare line this replaces.
    """
    def stop():
        return _stop_caller()

    def stop_live_runner_loop():
        return stop()

    def the_real_caller():
        return stop_live_runner_loop()

    assert "the_real_caller()" in the_real_caller()


def test_reports_interpreter_shutdown_distinctly(monkeypatch):
    """The case the evidence points at.

    The loop runs in the FOREGROUND of a Task Scheduler PowerShell, so ending
    the task ends the loop -- and that path leaves no other trace anywhere. It
    must not be reported as an ordinary in-process stop.
    """
    import sys

    monkeypatch.setattr(sys, "is_finalizing", lambda: True)
    assert _stop_caller() == "interpreter-shutdown"


def test_never_raises_on_the_teardown_path(monkeypatch):
    """A shutdown must not fail because the diagnostic failed."""
    import traceback

    monkeypatch.setattr(
        traceback, "extract_stack", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert _stop_caller() == "unknown-caller"


def test_stop_logs_at_warning_with_a_caller(caplog):
    """INFO is why 57 deaths scrolled past unnoticed."""
    loop = lrl.LiveRunnerLoop() if hasattr(lrl, "LiveRunnerLoop") else None
    if loop is None:  # pragma: no cover - class renamed
        import pytest

        pytest.skip("LiveRunnerLoop not exported under that name")

    loop._running = True  # make had_owner true without starting anything
    with caplog.at_level(logging.WARNING):
        loop.stop()

    stopped = [r for r in caplog.records if "stopped by" in r.getMessage()]
    assert stopped, "stop() must log who retired the loop"
    assert stopped[0].levelno == logging.WARNING
    assert "test_live_loop_stop_attribution.py" in stopped[0].getMessage()
