from __future__ import annotations

from scripts.run_counterfactual_replay_v3 import _cleanup_read_only_session


def test_read_only_cleanup_does_not_raise_or_skip_close(capsys):
    class LostConnectionSession:
        def __init__(self):
            self.closed = False

        def rollback(self):
            raise ConnectionError("connection already aborted")

        def close(self):
            self.closed = True

    db = LostConnectionSession()

    _cleanup_read_only_session(db)

    assert db.closed is True
    assert "read-only replay rollback cleanup failed: ConnectionError" in capsys.readouterr().err

