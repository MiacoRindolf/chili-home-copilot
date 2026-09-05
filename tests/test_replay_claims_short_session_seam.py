"""Gate #12: the claim helpers' short session follows the replay driver's transaction.

The production path (own connection, independent commit) is byte-identical when no provider is
installed; under ``replay_short_session_provider(db)`` the helper runs on ``db`` inside a
SAVEPOINT so it sees the driver's uncommitted session state and its own failure rolls back
only the savepoint."""
from __future__ import annotations

import pathlib

import pytest

from app.services.trading.momentum_neural import alpaca_orphan_claims as oc

_ROOT = pathlib.Path(__file__).resolve().parents[1]


class _Nested:
    def __init__(self, log):
        self.log = log

    def commit(self):
        self.log.append("savepoint_commit")

    def rollback(self):
        self.log.append("savepoint_rollback")


class _DriverSession:
    def __init__(self):
        self.log = []

    def begin_nested(self):
        self.log.append("begin_nested")
        return _Nested(self.log)

    def commit(self):
        self.log.append("OUTER_COMMIT")

    def close(self):
        self.log.append("OUTER_CLOSE")


class _ShortSession:
    instances = []

    def __init__(self):
        self.log = []
        _ShortSession.instances.append(self)

    def commit(self):
        self.log.append("commit")

    def rollback(self):
        self.log.append("rollback")

    def close(self):
        self.log.append("close")


def test_without_a_provider_the_helper_opens_its_own_session_and_commits(monkeypatch):
    import app.db as appdb
    _ShortSession.instances.clear()
    monkeypatch.setattr(appdb, "SessionLocal", _ShortSession)
    seen = []
    out = oc._with_short_session(lambda db: (seen.append(db), "ok")[1])
    assert out == "ok"
    assert len(_ShortSession.instances) == 1 and seen[0] is _ShortSession.instances[0]
    assert _ShortSession.instances[0].log == ["commit", "close"]


def test_under_the_seam_the_helper_runs_on_the_driver_session_inside_a_savepoint(monkeypatch):
    import app.db as appdb
    _ShortSession.instances.clear()
    monkeypatch.setattr(appdb, "SessionLocal", _ShortSession)
    drv = _DriverSession()
    seen = []
    with oc.replay_short_session_provider(drv):
        out = oc._with_short_session(lambda db: (seen.append(db), 7)[1])
    assert out == 7 and seen == [drv]
    assert drv.log == ["begin_nested", "savepoint_commit"]      # never the outer commit/close
    assert _ShortSession.instances == []                         # no second connection
    # the seam is scoped: after the block the production path is back
    oc._with_short_session(lambda db: None)
    assert len(_ShortSession.instances) == 1


def test_a_failing_helper_rolls_back_only_the_savepoint_and_reraises():
    drv = _DriverSession()

    def boom(db):
        raise RuntimeError("claim write failed")

    with oc.replay_short_session_provider(drv):
        with pytest.raises(RuntimeError):
            oc._with_short_session(boom)
    assert drv.log == ["begin_nested", "savepoint_rollback"]


def test_the_committed_wrappers_flow_through_the_seam(monkeypatch):
    # retire_deadman_handoff_reprotected_committed is the gate-#12 caller: it must reach the
    # driver session, not SessionLocal.
    drv = _DriverSession()
    calls = []
    monkeypatch.setattr(oc, "retire_deadman_handoff_reprotected",
                        lambda db, **kw: (calls.append(db), True)[1])
    with oc.replay_short_session_provider(drv):
        assert oc.retire_deadman_handoff_reprotected_committed(symbol="X") is True
    assert calls == [drv]


def test_the_replay_driver_installs_the_seam_around_run():
    body = (_ROOT / "scripts" / "replay_v3_fsm_window.py").read_text(encoding="utf-8")
    i = body.index("res = driver.run()")
    head = body[i - 400:i]
    assert "_oc.replay_short_session_provider(db)" in head
    assert "_gov.alpaca_account_snapshot_provider(mock.get_account_snapshot)" in head
