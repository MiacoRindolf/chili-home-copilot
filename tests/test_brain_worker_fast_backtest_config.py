from __future__ import annotations

from app.config import settings
from scripts import brain_worker

LEAN_CYCLE_MODE = "lean-cycle"
BACKTEST_MODE = "backtest"
LEAN_CYCLE_BATCH = 0
BACKTEST_BATCH = 30
EXPLICIT_DISABLED_BATCH = 0
CUSTOM_BACKTEST_BATCH = 17
BACKTEST_LOCK_SUFFIX = "brain_worker.backtest.lock"
LEAN_CYCLE_LOCK_NAME = "brain_worker.lock"
QUEUE_PENDING_BEFORE = 5
QUEUE_PENDING_AFTER = 2
QUEUE_PROMOTION_PATH_DEBT_BEFORE = 3
QUEUE_PROMOTION_PATH_DEBT_AFTER = 1
QUEUE_BACKTESTS_RUN = 11
QUEUE_PATTERNS_PROCESSED = 3
QUEUE_EXECUTOR = "process"
STATUS_TEST_PID = 4242
STATUS_THREAD_ONE = 111
STATUS_THREAD_TWO = 222


def test_fast_backtest_batch_defaults_are_mode_aware(monkeypatch):
    monkeypatch.delenv(brain_worker.FAST_BACKTEST_BATCH_ENV, raising=False)
    monkeypatch.setattr(
        settings,
        "brain_fast_backtest_batch_lean_cycle",
        LEAN_CYCLE_BATCH,
    )
    monkeypatch.setattr(
        settings,
        "brain_fast_backtest_batch_backtest",
        BACKTEST_BATCH,
    )

    lean = brain_worker._configure_fast_backtest_batch_for_mode(LEAN_CYCLE_MODE)
    assert lean["batch_size"] == LEAN_CYCLE_BATCH
    assert lean["source"] == brain_worker.FAST_BACKTEST_BATCH_SOURCE_MODE_DEFAULT

    monkeypatch.delenv(brain_worker.FAST_BACKTEST_BATCH_ENV, raising=False)
    backtest = brain_worker._configure_fast_backtest_batch_for_mode(BACKTEST_MODE)
    assert backtest["batch_size"] == BACKTEST_BATCH
    assert backtest["source"] == brain_worker.FAST_BACKTEST_BATCH_SOURCE_MODE_DEFAULT


def test_fast_backtest_batch_explicit_zero_override_wins(monkeypatch):
    monkeypatch.setenv(
        brain_worker.FAST_BACKTEST_BATCH_ENV,
        str(EXPLICIT_DISABLED_BATCH),
    )
    monkeypatch.setattr(
        settings,
        "brain_fast_backtest_batch_backtest",
        CUSTOM_BACKTEST_BATCH,
    )

    cfg = brain_worker._configure_fast_backtest_batch_for_mode(BACKTEST_MODE)

    assert cfg["batch_size"] == EXPLICIT_DISABLED_BATCH
    assert cfg["source"] == brain_worker.FAST_BACKTEST_BATCH_SOURCE_ENV


def test_worker_locks_are_mode_specific():
    lean_lock = brain_worker._lock_file_for_mode(LEAN_CYCLE_MODE)
    backtest_lock = brain_worker._lock_file_for_mode(BACKTEST_MODE)

    assert lean_lock.name == LEAN_CYCLE_LOCK_NAME
    assert backtest_lock.name == BACKTEST_LOCK_SUFFIX
    assert backtest_lock != lean_lock


def test_status_tmp_files_are_process_thread_specific(monkeypatch):
    monkeypatch.setattr(brain_worker.os, "getpid", lambda: STATUS_TEST_PID)
    monkeypatch.setattr(
        brain_worker.threading,
        "get_ident",
        lambda: STATUS_THREAD_ONE,
    )
    first = brain_worker._status_tmp_file()

    monkeypatch.setattr(
        brain_worker.threading,
        "get_ident",
        lambda: STATUS_THREAD_TWO,
    )
    second = brain_worker._status_tmp_file()

    assert first != second
    assert first.name == (
        f"{brain_worker.STATUS_FILE.name}."
        f"{STATUS_TEST_PID}.{STATUS_THREAD_ONE}{brain_worker.STATUS_TMP_SUFFIX}"
    )
    assert second.name == (
        f"{brain_worker.STATUS_FILE.name}."
        f"{STATUS_TEST_PID}.{STATUS_THREAD_TWO}{brain_worker.STATUS_TMP_SUFFIX}"
    )


def test_lean_cycle_fast_backtest_timer_stays_off_when_batch_is_zero(monkeypatch):
    monkeypatch.setattr(
        brain_worker,
        "_fast_backtest_batch_size",
        lambda: LEAN_CYCLE_BATCH,
    )

    assert (
        brain_worker._should_start_independent_fast_backtest_loop(
            LEAN_CYCLE_MODE,
            independent_loop=True,
        )
        is False
    )


def test_lean_cycle_fast_backtest_timer_can_run_when_explicitly_enabled(monkeypatch):
    monkeypatch.setattr(
        brain_worker,
        "_fast_backtest_batch_size",
        lambda: CUSTOM_BACKTEST_BATCH,
    )

    assert (
        brain_worker._should_start_independent_fast_backtest_loop(
            LEAN_CYCLE_MODE,
            independent_loop=True,
        )
        is True
    )


def test_backtest_mode_never_starts_lean_cycle_timer(monkeypatch):
    monkeypatch.setattr(
        brain_worker,
        "_fast_backtest_batch_size",
        lambda: BACKTEST_BATCH,
    )

    assert (
        brain_worker._should_start_independent_fast_backtest_loop(
            BACKTEST_MODE,
            independent_loop=True,
        )
        is False
    )


def test_fast_backtest_uses_shared_queue_executor(monkeypatch):
    from app.services.trading import learning

    class _Status:
        def set_step(self, *_args, **_kwargs):
            return None

    class _Db:
        rolled_back = False
        closed = False

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    db = _Db()
    snapshots = iter([
        {
            "pending": QUEUE_PENDING_BEFORE,
            "boosted": QUEUE_PENDING_BEFORE,
            "needs_retest": QUEUE_PENDING_BEFORE,
            "never_tested": 0,
            "promotion_path_debt_pending": QUEUE_PROMOTION_PATH_DEBT_BEFORE,
        },
        {
            "pending": QUEUE_PENDING_AFTER,
            "boosted": QUEUE_PENDING_AFTER,
            "needs_retest": QUEUE_PENDING_AFTER,
            "never_tested": 0,
            "promotion_path_debt_pending": QUEUE_PROMOTION_PATH_DEBT_AFTER,
        },
    ])
    calls: dict[str, object] = {}

    def _fake_queue_executor(session, user_id, *, batch_size):
        calls["session"] = session
        calls["user_id"] = user_id
        calls["batch_size"] = batch_size
        return {
            "backtests_run": QUEUE_BACKTESTS_RUN,
            "patterns_processed": QUEUE_PATTERNS_PROCESSED,
            "queue_executor": QUEUE_EXECUTOR,
        }

    monkeypatch.setattr(
        learning,
        "provider_egress_available_for_brain_work",
        lambda: True,
    )
    monkeypatch.setattr(learning, "_auto_backtest_from_queue", _fake_queue_executor)
    monkeypatch.setattr(
        settings,
        "brain_fast_backtest_skip_when_provider_down",
        False,
    )
    monkeypatch.setattr(settings, "brain_default_user_id", None)
    monkeypatch.setattr(brain_worker, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        brain_worker,
        "_fast_backtest_queue_status_snapshot",
        lambda: next(snapshots),
    )
    monkeypatch.setattr(
        brain_worker,
        "_due_brain_work_backtest_requests_snapshot",
        lambda: {"due": 0, "by_source": {}, "oldest": None},
    )
    monkeypatch.setattr(
        brain_worker,
        "_fast_backtest_batch_size",
        lambda: BACKTEST_BATCH,
    )
    monkeypatch.setattr(
        brain_worker,
        "_fast_backtest_executor_label",
        lambda: QUEUE_EXECUTOR,
    )

    out = brain_worker._run_subtask_fast_backtest(_Status())

    assert calls["session"] is db
    assert calls["batch_size"] == BACKTEST_BATCH
    assert out["completed"] == QUEUE_BACKTESTS_RUN
    assert out["processed_patterns"] == QUEUE_PATTERNS_PROCESSED
    assert out["queue_executor"] == QUEUE_EXECUTOR
    assert out["pending_before"] == QUEUE_PENDING_BEFORE
    assert out["pending_after"] == QUEUE_PENDING_AFTER
    assert (
        out["promotion_path_debt_pending_before"]
        == QUEUE_PROMOTION_PATH_DEBT_BEFORE
    )
    assert out["promotion_path_debt_pending_after"] == QUEUE_PROMOTION_PATH_DEBT_AFTER
    assert db.rolled_back is True
    assert db.closed is True


def test_fast_backtest_stands_down_when_durable_backtest_work_is_due(
    monkeypatch,
) -> None:
    from app.services.trading import learning

    class _Status:
        def set_step(self, *_args, **_kwargs):
            return None

    called = []

    def _should_not_run(*_args, **_kwargs):
        called.append(True)
        raise AssertionError("generic queue executor should stand down")

    monkeypatch.setattr(
        learning,
        "provider_egress_available_for_brain_work",
        lambda: True,
    )
    monkeypatch.setattr(learning, "_auto_backtest_from_queue", _should_not_run)
    monkeypatch.setattr(
        settings,
        "brain_fast_backtest_skip_when_provider_down",
        False,
    )
    monkeypatch.setattr(
        brain_worker,
        "_fast_backtest_queue_status_snapshot",
        lambda: {
            "pending": QUEUE_PENDING_BEFORE,
            "boosted": QUEUE_PENDING_BEFORE,
            "needs_retest": QUEUE_PENDING_BEFORE,
            "never_tested": 0,
            "promotion_path_debt_pending": QUEUE_PROMOTION_PATH_DEBT_BEFORE,
        },
    )
    monkeypatch.setattr(
        brain_worker,
        "_due_brain_work_backtest_requests_snapshot",
        lambda: {
            "due": 2,
            "by_source": {"recert_rescue_refresh": 2},
            "oldest": "2026-05-30T00:18:24",
        },
    )
    monkeypatch.setattr(
        brain_worker,
        "_fast_backtest_batch_size",
        lambda: BACKTEST_BATCH,
    )

    out = brain_worker._run_subtask_fast_backtest(_Status())

    assert called == []
    assert out["skipped"] is True
    assert out["skip_reason"] == "brain_work_backtest_requests_due"
    assert out["durable_backtest_due"] == 2
    assert out["durable_backtest_by_source"] == {"recert_rescue_refresh": 2}
    assert out["pending_before"] == QUEUE_PENDING_BEFORE
    assert out["pending_after"] == QUEUE_PENDING_BEFORE
