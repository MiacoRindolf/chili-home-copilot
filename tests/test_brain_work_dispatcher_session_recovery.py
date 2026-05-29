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


def test_isolated_mesh_publish_rolls_back_private_session(monkeypatch) -> None:
    """A swallowed mesh DB failure is contained in the helper-owned session."""
    from app.services.trading.brain_neural_mesh import publisher
    from app.services.trading.brain_work import dispatcher

    class FakeSession:
        def __init__(self) -> None:
            self.poisoned = False
            self.commits = 0
            self.rollbacks = 0
            self.closed = False

        def commit(self) -> None:
            self.commits += 1
            if self.poisoned:
                raise RuntimeError("mesh transaction poisoned")

        def rollback(self) -> None:
            self.rollbacks += 1

        def close(self) -> None:
            self.closed = True

    session = FakeSession()
    monkeypatch.setattr("app.db.SessionLocal", lambda: session)

    def swallowed_poisoned_publish(mesh_db, **_kwargs) -> None:
        assert mesh_db is session
        mesh_db.poisoned = True

    monkeypatch.setattr(publisher, "publish_brain_work_outcome", swallowed_poisoned_publish)

    dispatcher._publish_brain_work_outcome_isolated(
        outcome_type="execution_quality_updated",
        scan_pattern_id=None,
        extra={"work_event_id": 1},
    )

    assert session.commits == 1
    assert session.rollbacks == 1
    assert session.closed is True


def test_backtest_handler_rolls_back_dispatch_session_before_done(monkeypatch) -> None:
    """Long backtests use separate sessions, so clear the dispatcher session before mark-done."""
    from types import SimpleNamespace

    from app.services.trading.brain_work import dispatcher

    class FakeSession:
        def __init__(self) -> None:
            self.commits = 0
            self.rollbacks = 0
            self.closed = False

        def get(self, _model, _pid):
            return SimpleNamespace(
                promotion_status="candidate",
                lifecycle_stage="pilot_promoted",
            )

        def commit(self) -> None:
            self.commits += 1

        def rollback(self) -> None:
            self.rollbacks += 1

        def close(self) -> None:
            self.closed = True

    opened_sessions: list[FakeSession] = []

    def session_factory() -> FakeSession:
        sess = FakeSession()
        opened_sessions.append(sess)
        return sess

    class DispatchSession:
        def __init__(self) -> None:
            self.rollbacks = 0

        def rollback(self) -> None:
            self.rollbacks += 1

    dispatch_db = DispatchSession()
    monkeypatch.setattr("app.db.SessionLocal", session_factory)
    monkeypatch.setattr(
        "app.services.trading.backtest_queue_worker.execute_queue_backtest_for_pattern",
        lambda _pid, _uid: (3, 3),
    )
    monkeypatch.setattr(dispatcher, "enqueue_outcome_event", lambda *a, **kw: 1)
    monkeypatch.setattr(dispatcher, "emit_promotion_surface_change", lambda *a, **kw: None)
    monkeypatch.setattr(dispatcher, "_publish_brain_work_outcome_isolated", lambda **kw: None)

    ev = SimpleNamespace(id=42, payload={"scan_pattern_id": 1256})
    dispatcher._handle_backtest_requested(dispatch_db, ev, user_id=None)

    assert dispatch_db.rollbacks == 1
    assert len(opened_sessions) == 2
    assert all(sess.closed for sess in opened_sessions)
