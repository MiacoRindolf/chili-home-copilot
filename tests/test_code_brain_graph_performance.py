from app.services.code_brain.graph import _find_cycles, _top_coupling_rows, _top_degree_items


def test_top_degree_items_uses_bounded_top_n_with_stable_ties() -> None:
    items = [
        ("first", 3),
        ("second", 9),
        ("third", 9),
        ("fourth", 1),
        ("fifth", 7),
    ]

    assert _top_degree_items(items, 3) == [
        ("second", 9),
        ("third", 9),
        ("fifth", 7),
    ]


def test_top_degree_items_empty_for_non_positive_limit() -> None:
    assert _top_degree_items([("first", 3)], 0) == []


def test_top_coupling_rows_uses_bounded_top_n_with_stable_ties() -> None:
    rows = [
        {"source_dir": "a", "target_dir": "x", "edge_count": 1},
        {"source_dir": "b", "target_dir": "x", "edge_count": 9},
        {"source_dir": "c", "target_dir": "x", "edge_count": 9},
        {"source_dir": "d", "target_dir": "x", "edge_count": 5},
        {"source_dir": "e", "target_dir": "x", "edge_count": 2},
    ]

    assert _top_coupling_rows(rows, 3) == [
        {"source_dir": "b", "target_dir": "x", "edge_count": 9},
        {"source_dir": "c", "target_dir": "x", "edge_count": 9},
        {"source_dir": "d", "target_dir": "x", "edge_count": 5},
    ]


def test_top_coupling_rows_empty_for_non_positive_limit() -> None:
    assert _top_coupling_rows([{"edge_count": 3}], 0) == []


def test_find_cycles_preserves_back_edge_cycle_shape() -> None:
    graph = {
        "a": ["b"],
        "b": ["c"],
        "c": ["d"],
        "d": ["b"],
    }

    assert _find_cycles(graph) == [["b", "c", "d", "b"]]
