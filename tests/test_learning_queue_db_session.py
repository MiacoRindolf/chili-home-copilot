from __future__ import annotations

from app.services.trading.learning import (
    _get_queue_status_after_batch,
    _release_queue_parent_session,
)


class _FakeSession:
    def __init__(self, *, fail_rollback: bool = False) -> None:
        self.fail_rollback = fail_rollback
        self.rollback_calls = 0
        self.close_calls = 0

    def rollback(self) -> None:
        self.rollback_calls += 1
        if self.fail_rollback:
            raise RuntimeError("rollback failed")

    def close(self) -> None:
        self.close_calls += 1


def test_release_queue_parent_session_rolls_back_read_transaction():
    db = _FakeSession()

    _release_queue_parent_session(db, reason="unit test")

    assert db.rollback_calls == 1
    assert db.close_calls == 0


def test_release_queue_parent_session_closes_when_rollback_fails():
    db = _FakeSession(fail_rollback=True)

    _release_queue_parent_session(db, reason="unit test")

    assert db.rollback_calls == 1
    assert db.close_calls == 1


def test_queue_status_after_batch_recycles_session_once_on_stale_socket():
    db = _FakeSession()
    calls = 0

    def flaky_status(_db, *, use_cache: bool) -> dict:
        nonlocal calls
        assert use_cache is False
        calls += 1
        if calls == 1:
            raise RuntimeError("server closed the connection unexpectedly")
        return {"queue_empty": False, "pending": 7}

    status = _get_queue_status_after_batch(db, flaky_status)

    assert status == {"queue_empty": False, "pending": 7}
    assert calls == 2
    assert db.rollback_calls == 1


def test_queue_status_after_batch_degrades_after_retry_failure():
    db = _FakeSession()

    def broken_status(_db, *, use_cache: bool) -> dict:
        raise RuntimeError("server closed the connection unexpectedly")

    status = _get_queue_status_after_batch(db, broken_status)

    assert status["queue_empty"] is False
    assert status["queue_status_degraded"] is True
    assert "server closed" in status["queue_status_error"]
    assert db.rollback_calls == 1
