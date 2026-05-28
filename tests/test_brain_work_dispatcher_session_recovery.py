from __future__ import annotations

from sqlalchemy import text

from app.models.trading import BrainWorkEvent
from app.services.trading.brain_work.dispatcher import run_brain_work_dispatch_round
from app.services.trading.brain_work.ledger import enqueue_work_event


def test_dispatcher_recovers_after_swallowed_db_handler_failure(db, monkeypatch) -> None:
    """A swallowed DB error must not leave the dispatcher transaction poisoned."""
    from app.services.trading.brain_work.handlers import cpcv_gate, quality_score

    monkeypatch.setattr(
        cpcv_gate,
        "handle_backtest_completed",
        lambda db_arg, ev, user_id: None,
    )

    def poison_dispatch_session(db_arg, ev, user_id) -> None:
        db_arg.execute(text("SELECT * FROM definitely_missing_brain_work_table"))

    monkeypatch.setattr(
        quality_score,
        "handle_backtest_completed_quality",
        poison_dispatch_session,
    )

    event_id = enqueue_work_event(
        db,
        event_type="backtest_completed",
        dedupe_key="bt_done:dispatcher-session-recovery",
        payload={"scan_pattern_id": 537},
        max_attempts=1,
    )
    db.commit()
    assert event_id is not None

    result = run_brain_work_dispatch_round(
        db,
        max_backtest=0,
        max_exec_feedback=0,
        max_edge_reliability=0,
        max_recert_rescue=0,
        max_exit_variant=0,
        max_provenance=0,
        max_mine=0,
        max_cpcv_gate=1,
        max_promote=0,
        max_trade_close=0,
        run_thin_evidence_sweep=False,
        run_market_snapshots_watchdog=False,
    )
    db.commit()

    row = db.get(BrainWorkEvent, event_id)
    assert result["processed"] == 1
    assert result["errors"] == []
    assert row is not None
    assert row.status == "done"
    assert row.last_error in (None, "")
