class _FakeSession:
    def __init__(self, name: str) -> None:
        self.name = name
        self.rollbacks = 0
        self.invalidates = 0
        self.closed = False

    def rollback(self) -> None:
        self.rollbacks += 1

    def invalidate(self) -> None:
        self.invalidates += 1

    def close(self) -> None:
        self.closed = True


def test_brain_work_batch_retries_once_after_disconnect(monkeypatch):
    import scripts.brain_worker as brain_worker

    primary = _FakeSession("primary")
    retry = _FakeSession("retry")
    sessions = [primary, retry]
    calls: list[str] = []

    monkeypatch.setattr(brain_worker, "SessionLocal", lambda: sessions.pop(0))

    def _run_brain_work_dispatch_round(db, *, user_id=None, **dispatch_kwargs):
        calls.append(db.name)
        assert dispatch_kwargs == {}
        if db is primary:
            raise RuntimeError("server closed the connection unexpectedly")
        return {"processed": 0, "claimed": 0, "per_type": {}, "errors": []}

    monkeypatch.setattr(
        brain_worker,
        "_run_brain_work_dispatch_round",
        _run_brain_work_dispatch_round,
    )

    brain_worker._maybe_run_brain_work_batch()

    assert calls == ["primary", "retry"]
    assert primary.invalidates == 1
    assert primary.closed is True
    assert retry.invalidates == 0
    assert retry.closed is True
