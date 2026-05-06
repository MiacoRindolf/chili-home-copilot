"""Tests for f-fix-backtest-completed-emitter (Phase 5 of f-overnight-cleanup)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1. New emitter exists with the expected signature
# ---------------------------------------------------------------------------

def test_emit_backtest_completed_outcome_exists():
    from app.services.trading.brain_work.emitters import (
        emit_backtest_completed_outcome,
    )
    import inspect
    sig = inspect.signature(emit_backtest_completed_outcome)
    params = set(sig.parameters)
    # scan_pattern_id is the only required arg per cpcv_gate's contract.
    assert "scan_pattern_id" in params
    assert "user_id" in params
    assert "backtests_run" in params
    assert "win_rate" in params
    assert "avg_return" in params


# ---------------------------------------------------------------------------
# 2. backtest_queue_worker references the emit + wraps in try/except
# ---------------------------------------------------------------------------

def test_completion_site_references_emit():
    src = (REPO / "app/services/trading/backtest_queue_worker.py").read_text()
    assert "emit_backtest_completed_outcome" in src, (
        "backtest_queue_worker.py must reference emit_backtest_completed_outcome"
    )
    # Pin the try/except wrapper.
    idx = src.find("emit_backtest_completed_outcome(")
    assert idx >= 0
    preceding = src[max(0, idx - 800):idx]
    assert "try:" in preceding, (
        "emit_backtest_completed_outcome call must be in a try: block"
    )


# ---------------------------------------------------------------------------
# 3. cpcv_gate handler still subscribes to backtest_completed (regression)
# ---------------------------------------------------------------------------

def test_cpcv_gate_still_subscribes_to_backtest_completed():
    src = (REPO / "app/services/trading/brain_work/dispatcher.py").read_text()
    assert "\"backtest_completed\"" in src, (
        "dispatcher.py must still dispatch backtest_completed events"
    )
    src2 = (
        REPO / "app/services/trading/brain_work/handlers/cpcv_gate.py"
    ).read_text()
    assert "handle_backtest_completed" in src2


# ---------------------------------------------------------------------------
# 4. Emitter call writes a brain_work_events row when invoked directly
#    (smoke against chili_test, paying the per-test truncate cost once)
# ---------------------------------------------------------------------------

def test_emitter_writes_brain_work_event(db):
    from sqlalchemy import text
    from app.services.trading.brain_work.emitters import (
        emit_backtest_completed_outcome,
    )

    emit_backtest_completed_outcome(
        db,
        scan_pattern_id=12345,
        user_id=None,
        backtests_run=7,
        win_rate=0.6,
        avg_return=1.2,
    )
    db.commit()

    rows = db.execute(text(
        "SELECT event_type, payload FROM brain_work_events "
        "WHERE event_type = 'backtest_completed' "
        "  AND payload->>'scan_pattern_id' = '12345'"
    )).all()
    assert len(rows) >= 1
    et, payload = rows[0]
    assert et == "backtest_completed"
    assert payload.get("scan_pattern_id") == 12345
    assert payload.get("backtests_run") == 7
    assert payload.get("win_rate") == 0.6
    assert payload.get("avg_return") == 1.2
