from __future__ import annotations

from types import SimpleNamespace

from app.services.trading.momentum_neural import ab_test
from app.services.trading.momentum_neural.ab_test import (
    _ab_pair_row_fields,
    _comparison_from_return_slices,
    _return_slices_by_variant_from_rows,
    _return_slices_from_peer_rows,
)


def test_return_slices_from_peer_rows_groups_tuple_and_object_rows() -> None:
    rows = [
        (7, 10.0),
        SimpleNamespace(variant_id=8, return_bps=-20.0),
        (7, 30.0),
        (9, 999.0),
    ]

    a, b = _return_slices_from_peer_rows(rows, variant_a_id=7, variant_b_id=8)

    assert a == [10.0, 30.0]
    assert b == [-20.0]


def test_return_slices_from_peer_rows_handles_same_variant_peer() -> None:
    rows = [(7, 10.0), (7, -20.0)]

    a, b = _return_slices_from_peer_rows(rows, variant_a_id=7, variant_b_id=7)

    assert a == [10.0, -20.0]
    assert b == [10.0, -20.0]


def test_return_slices_by_variant_from_rows_keeps_requested_ids() -> None:
    rows = [
        (7, 10.0),
        SimpleNamespace(variant_id=8, return_bps=-20.0),
        (9, 999.0),
    ]

    out = _return_slices_by_variant_from_rows(rows, variant_ids=[7, 8, 10])

    assert out == {
        7: [10.0],
        8: [-20.0],
        10: [],
    }


def test_comparison_from_return_slices_preserves_ready_and_winner_semantics() -> None:
    out = _comparison_from_return_slices(
        variant_a_id=7,
        variant_b_id=8,
        a=[10.0, 20.0, 30.0],
        b=[5.0, -5.0, 0.0],
        min_sessions=3,
    )

    assert out == {
        "variant_a_id": 7,
        "variant_b_id": 8,
        "a_n": 3,
        "b_n": 3,
        "a_mean_bps": 20.0,
        "b_mean_bps": 0.0,
        "winner": "a",
        "ready": True,
    }


def test_comparison_from_return_slices_not_ready_without_min_sessions() -> None:
    out = _comparison_from_return_slices(
        variant_a_id=7,
        variant_b_id=8,
        a=[20.0],
        b=[100.0, 120.0],
        min_sessions=2,
    )

    assert out["ready"] is False
    assert out["winner"] is None
    assert out["a_mean_bps"] == 20.0


def test_ab_pair_row_fields_handles_tuple_and_object_rows() -> None:
    assert _ab_pair_row_fields((7, "Parent", {"ab_peer_variant_id": 8})) == (
        7,
        "Parent",
        {"ab_peer_variant_id": 8},
    )
    assert _ab_pair_row_fields(
        SimpleNamespace(id=9, label="Child", refinement_meta_json={"ab_role": "child"})
    ) == (9, "Child", {"ab_role": "child"})
    assert _ab_pair_row_fields((10, None, None)) == (10, "", {})


def test_list_ab_pairs_batches_peer_return_slices(monkeypatch) -> None:
    class FakeQuery:
        def __init__(self, rows):
            self.rows = rows

        def filter(self, *_args, **_kwargs):
            return self

        def order_by(self, *_args, **_kwargs):
            return self

        def limit(self, *_args, **_kwargs):
            return self

        def all(self):
            return self.rows

    class FakeDb:
        def __init__(self, rows):
            self.rows = rows
            self.query_count = 0
            self.full_variant_query_count = 0
            self.column_variant_query_count = 0

        def query(self, *args):
            self.query_count += 1
            if len(args) == 1:
                self.full_variant_query_count += 1
            if len(args) == 3:
                self.column_variant_query_count += 1
            return FakeQuery(self.rows)

    rows = [
        (7, "Parent", {"ab_peer_variant_id": 8, "ab_role": "parent"}),
        (9, "Child", {"ab_peer_variant_id": 10, "ab_role": "child"}),
    ]
    calls: list[list[int]] = []

    def fake_latest(_db, *, variant_ids, min_sessions=5, days=30):
        calls.append(list(variant_ids))
        assert min_sessions == 5
        assert days == 30
        return {
            7: [20.0, 10.0, 30.0, 15.0, 25.0],
            8: [0.0, -5.0, 5.0, -10.0, 10.0],
            9: [-10.0],
            10: [50.0, 40.0, 30.0, 20.0, 10.0],
        }

    monkeypatch.setattr(ab_test, "_latest_return_slices_by_variant", fake_latest)
    monkeypatch.setattr(
        ab_test,
        "compare_peer_variants",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("per-pair compare should not run")),
    )

    db = FakeDb(rows)
    out = ab_test.list_ab_pairs(db, limit=20)

    assert db.full_variant_query_count == 0
    assert db.column_variant_query_count == 1
    assert calls == [[7, 8, 9, 10]]
    assert out[0]["comparison"]["winner"] == "a"
    assert out[1]["comparison"]["ready"] is False
