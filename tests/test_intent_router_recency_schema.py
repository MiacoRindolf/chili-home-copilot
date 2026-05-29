from app.services.context_brain import intent_router
from app.services.context_brain.types import INTENT_CODE


class _NoExecuteDb:
    def execute(self, *_args, **_kwargs):
        raise AssertionError("_recency_signal should not query chat_logs.user_id")


class _Rows:
    rowcount = 0

    def fetchall(self):
        return [("code_review", 2)]


class _CaptureDb:
    def __init__(self):
        self.sql = ""
        self.params = None

    def execute(self, statement, params=None):
        self.sql = str(statement)
        self.params = params
        return _Rows()


def test_recency_signal_skips_when_chat_logs_user_id_is_absent(monkeypatch):
    monkeypatch.setattr(
        intent_router,
        "_chat_logs_columns",
        lambda _db: {"id", "created_at", "action_type"},
    )

    assert intent_router._recency_signal(_NoExecuteDb(), 7) == {}


def test_recency_signal_uses_user_filter_when_schema_supports_it(monkeypatch):
    monkeypatch.setattr(
        intent_router,
        "_chat_logs_columns",
        lambda _db: {"id", "created_at", "action_type", "user_id"},
    )
    db = _CaptureDb()

    signals = intent_router._recency_signal(db, 42)

    assert signals == {INTENT_CODE: 0.3}
    assert "WHERE user_id = :uid" in db.sql
    assert db.params == {"uid": 42}
