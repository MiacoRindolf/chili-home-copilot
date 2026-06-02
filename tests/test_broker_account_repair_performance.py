from __future__ import annotations

from types import SimpleNamespace

from app.services.trading import broker_account_repair as repair


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return list(self._rows)


class _FakeDb:
    def __init__(self, rows):
        self._rows = rows
        self.queried = None

    def query(self, *_args, **_kwargs):
        self.queried = _args
        return _FakeQuery(self._rows)


def test_choose_canonical_user_id_uses_min_without_sorting(monkeypatch) -> None:
    def fail_sort(*_args, **_kwargs):
        raise AssertionError("canonical user selection should not sort eligible ids")

    monkeypatch.setattr(repair, "sorted", fail_sort, raising=False)
    db = _FakeDb([
        SimpleNamespace(id=9, email="", name="trader-9"),
        SimpleNamespace(id=3, email="", name="Alice"),
        SimpleNamespace(id=5, email="alice@example.com", name="Trader"),
    ])

    assert repair._choose_canonical_user_id(db, [9, 3, 5]) == 5
    assert [getattr(col, "key", None) for col in db.queried] == ["id", "email", "name"]


def test_choose_canonical_user_id_falls_back_to_smallest_id_without_sorting(monkeypatch) -> None:
    def fail_sort(*_args, **_kwargs):
        raise AssertionError("canonical fallback should not sort all ids")

    monkeypatch.setattr(repair, "sorted", fail_sort, raising=False)
    db = _FakeDb([
        SimpleNamespace(id=9, email="", name="trader-9"),
        SimpleNamespace(id=3, email="", name="trader-3"),
    ])

    assert repair._choose_canonical_user_id(db, [9, 3, 7]) == 3


def test_choose_canonical_user_id_supports_compact_tuple_rows() -> None:
    db = _FakeDb([
        (9, "", "trader-9"),
        (3, "", "Alice"),
        (5, "alice@example.com", "Trader"),
    ])

    assert repair._choose_canonical_user_id(db, [9, 3, 5]) == 5
    assert [getattr(col, "key", None) for col in db.queried] == ["id", "email", "name"]


def test_user_identity_field_supports_object_tuple_and_mapping_rows() -> None:
    obj = SimpleNamespace(id=1, email="a@example.com", name="A")
    mapping = {"id": 2, "email": "", "name": "trader-2"}

    assert repair._user_identity_field(obj, "email", 1) == "a@example.com"
    assert repair._user_identity_field((3, "b@example.com", "B"), "name", 2) == "B"
    assert repair._user_identity_field(mapping, "id", 0) == 2
