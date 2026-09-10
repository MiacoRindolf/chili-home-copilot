"""2026-09-10: the arm pass skipped with ``loss_guard_history_unavailable`` for 2 h 26 min of RTH and
no log line said WHY. The reason, the gap counts and the gap session ids were all in
``out["loss_guard_history"]`` and never reached a log. Both log sites now append
``loss_guard_skip_detail(out)``; the scheduler's change-only line is WARNING for that skip."""
from __future__ import annotations

import inspect
import logging

import pytest

from app.services import trading_scheduler as TS
from app.services.trading.momentum_neural import auto_arm as AA


def _todays_out() -> dict:
    # the exact shape run_auto_arm_pass returned all afternoon
    return {
        "skipped": "loss_guard_history_unavailable",
        "loss_guard_history": {
            "schema_version": "chili.current-live-loss-history.v2",
            "reason": "loss_guard_outcome_session_terminal_clock_mismatch",
            "history_unavailable": True,
            "coverage_grade": "COVERAGE_UNAVAILABLE",
            "coverage_gap_counts": {"loss_guard_outcome_session_terminal_clock_mismatch": 1},
            "coverage_gap_session_ids": [21605],
        },
    }


def test_detail_carries_reason_gaps_and_session_ids():
    d = AA.loss_guard_skip_detail(_todays_out())
    assert "reason=loss_guard_outcome_session_terminal_clock_mismatch" in d
    assert "gap_session_ids=[21605]" in d
    assert "loss_guard_outcome_session_terminal_clock_mismatch': 1" in d


def test_detail_carries_error_type_for_the_exception_branches():
    out = {
        "skipped": "loss_guard_history_unavailable",
        "loss_guard_history": {
            "reason": "loss_guard_pre_history_flush_unavailable",
            "error_type": "PendingRollbackError",
        },
    }
    d = AA.loss_guard_skip_detail(out)
    assert "reason=loss_guard_pre_history_flush_unavailable" in d
    assert "error_type=PendingRollbackError" in d


@pytest.mark.parametrize("out", [
    None,
    {},
    {"skipped": "no_active_trigger"},
    {"skipped": None, "armed": 1},
])
def test_detail_is_empty_for_every_other_skip(out):
    assert AA.loss_guard_skip_detail(out) == ""


def test_detail_never_raises_on_garbage():
    assert AA.loss_guard_skip_detail({"skipped": "loss_guard_history_unavailable", "loss_guard_history": object()}) != ""


def test_both_log_sites_call_the_detail_helper():
    bridge_src = inspect.getsource(AA)
    i = bridge_src.index("ignition→arm bridge: symbols=%s armed=%s skipped=%s")
    assert "loss_guard_skip_detail(out)" in bridge_src[i:i + 600]
    sched_src = inspect.getsource(TS._run_momentum_auto_arm_live_job)
    assert "loss_guard_skip_detail(summary)" in sched_src
    assert "logging.WARNING if _lg_detail else logging.INFO" in sched_src


def test_scheduler_skip_line_is_warning_for_loss_guard_and_info_otherwise(caplog):
    """Drive the exact logging call the scheduler makes, with the real helper."""
    caplog.set_level(logging.INFO, logger=TS.logger.name)
    for summary, level in ((_todays_out(), logging.WARNING), ({"skipped": "no_active_trigger"}, logging.INFO)):
        caplog.clear()
        detail = AA.loss_guard_skip_detail(summary)
        TS.logger.log(
            logging.WARNING if detail else logging.INFO,
            "[scheduler] auto_arm skip=%s scanned=%s busy=%s faded=%s near_score=%s firing=%s%s",
            summary.get("skipped"), None, None, None, None, None, detail,
        )
        rec = caplog.records[-1]
        assert rec.levelno == level
        if level == logging.WARNING:
            assert "gap_session_ids=[21605]" in rec.getMessage()
