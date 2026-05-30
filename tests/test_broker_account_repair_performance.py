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

    def query(self, *_args, **_kwargs):
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


def test_choose_canonical_user_id_falls_back_to_smallest_id_without_sorting(monkeypatch) -> None:
    def fail_sort(*_args, **_kwargs):
        raise AssertionError("canonical fallback should not sort all ids")

    monkeypatch.setattr(repair, "sorted", fail_sort, raising=False)
    db = _FakeDb([
        SimpleNamespace(id=9, email="", name="trader-9"),
        SimpleNamespace(id=3, email="", name="trader-3"),
    ])

    assert repair._choose_canonical_user_id(db, [9, 3, 7]) == 3
