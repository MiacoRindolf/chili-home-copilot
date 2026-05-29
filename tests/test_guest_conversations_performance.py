from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from app.models import ChatMessage
from app.routers import chat


def _query_is_chat_message(args: tuple[object, ...]) -> bool:
    return len(args) == 1 and args[0] is ChatMessage


class _FakeQuery:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows
        self.filter_calls = 0
        self.order_by_calls = 0
        self.group_by_calls = 0

    def filter(self, *args: object) -> "_FakeQuery":
        self.filter_calls += 1
        return self

    def group_by(self, *args: object) -> "_FakeQuery":
        self.group_by_calls += 1
        return self

    def order_by(self, *args: object) -> "_FakeQuery":
        self.order_by_calls += 1
        return self

    def all(self) -> list[object]:
        return self.rows


class _FakeSession:
    def __init__(
        self,
        *,
        guest_rows: list[tuple[str, int, datetime]] | None = None,
        message_rows: list[SimpleNamespace] | None = None,
    ) -> None:
        self.guest_rows = guest_rows or []
        self.message_rows = message_rows or []
        self.queries: list[tuple[tuple[object, ...], _FakeQuery]] = []

    def query(self, *args: object) -> _FakeQuery:
        if _query_is_chat_message(args):
            query = _FakeQuery(self.message_rows)
        else:
            query = _FakeQuery(self.guest_rows)
        self.queries.append((args, query))
        return query


def test_first_guest_user_messages_by_convo_key_batches_lookup() -> None:
    first = SimpleNamespace(convo_key="guest:a", content="hello")
    duplicate = SimpleNamespace(convo_key="guest:a", content="later")
    other = SimpleNamespace(convo_key="guest:b", content="hi")
    db = _FakeSession(message_rows=[first, duplicate, other])

    result = chat._first_guest_user_messages_by_convo_key(  # type: ignore[arg-type]
        db,
        {"guest:a", "guest:b"},
    )

    assert result == {"guest:a": first, "guest:b": other}
    assert len(db.queries) == 1
    assert _query_is_chat_message(db.queries[0][0])
    assert db.queries[0][1].filter_calls == 1
    assert db.queries[0][1].order_by_calls == 1


def test_first_guest_user_messages_by_convo_key_skips_empty_lookup() -> None:
    db = _FakeSession()

    assert chat._first_guest_user_messages_by_convo_key(db, set()) == {}  # type: ignore[arg-type]
    assert db.queries == []


def test_list_guest_conversations_batches_titles(monkeypatch) -> None:
    now = datetime(2026, 5, 28, 21, 0)
    db = _FakeSession(
        guest_rows=[("guest:a", 3, now), ("guest:b", 2, now)],
        message_rows=[
            SimpleNamespace(convo_key="guest:a", content="First guest prompt"),
            SimpleNamespace(convo_key="guest:b", content="Second guest prompt"),
        ],
    )
    request = SimpleNamespace(cookies={chat.DEVICE_COOKIE_NAME: "paired-device"})
    monkeypatch.setattr(chat, "get_identity_record", lambda _db, _token: {"is_guest": False})

    result = chat.list_guest_conversations(request, db)  # type: ignore[arg-type]

    assert result == {
        "guest_conversations": [
            {
                "convo_key": "guest:a",
                "title": "First guest prompt",
                "msg_count": 3,
                "last_active": str(now),
            },
            {
                "convo_key": "guest:b",
                "title": "Second guest prompt",
                "msg_count": 2,
                "last_active": str(now),
            },
        ]
    }
    assert len(db.queries) == 2
    assert _query_is_chat_message(db.queries[1][0])
