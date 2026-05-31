from app.services.code_brain import graph
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


def test_python_import_resolution_reuses_cached_filesystem_lookup(tmp_path, monkeypatch) -> None:
    graph._resolve_python_import.cache_clear()
    source_dir = tmp_path / "app" / "services"
    source_dir.mkdir(parents=True)
    (source_dir / "target.py").write_text("VALUE = 1\n", encoding="utf-8")

    assert graph._resolve_python_import("app.services.target", tmp_path, source_dir) == "app/services/target.py"

    def fail_isfile(*_args, **_kwargs):
        raise AssertionError("cached import resolution should skip filesystem probes")

    monkeypatch.setattr(graph.os.path, "isfile", fail_isfile)

    assert graph._resolve_python_import("app.services.target", tmp_path, source_dir) == "app/services/target.py"
    assert graph._resolve_python_import.cache_info().hits == 1


def test_python_import_resolution_cache_is_bounded() -> None:
    assert graph._resolve_python_import.cache_info().maxsize == 8192


def test_python_import_resolution_cache_clear_observes_new_files(tmp_path) -> None:
    graph._resolve_python_import.cache_clear()
    source_dir = tmp_path / "app" / "services"
    source_dir.mkdir(parents=True)

    assert graph._resolve_python_import("app.services.later", tmp_path, source_dir) is None
    (source_dir / "later.py").write_text("VALUE = 1\n", encoding="utf-8")
    assert graph._resolve_python_import("app.services.later", tmp_path, source_dir) is None

    graph._resolve_python_import.cache_clear()

    assert graph._resolve_python_import("app.services.later", tmp_path, source_dir) == "app/services/later.py"


def test_js_import_resolution_reuses_cached_filesystem_lookup(tmp_path, monkeypatch) -> None:
    graph._resolve_js_import.cache_clear()
    source_dir = tmp_path / "web" / "src"
    source_dir.mkdir(parents=True)
    (source_dir / "component.ts").write_text("export const value = 1\n", encoding="utf-8")

    assert graph._resolve_js_import("./component", tmp_path, source_dir) == "web/src/component.ts"

    def fail_isfile(*_args, **_kwargs):
        raise AssertionError("cached JS import resolution should skip filesystem probes")

    monkeypatch.setattr(graph.os.path, "isfile", fail_isfile)

    assert graph._resolve_js_import("./component", tmp_path, source_dir) == "web/src/component.ts"
    assert graph._resolve_js_import.cache_info().hits == 1


def test_js_import_resolution_cache_is_bounded() -> None:
    assert graph._resolve_js_import.cache_info().maxsize == 8192


def test_js_import_resolution_cache_clear_observes_new_files(tmp_path) -> None:
    graph._resolve_js_import.cache_clear()
    source_dir = tmp_path / "web" / "src"
    source_dir.mkdir(parents=True)

    assert graph._resolve_js_import("./later", tmp_path, source_dir) is None
    (source_dir / "later.ts").write_text("export const value = 1\n", encoding="utf-8")
    assert graph._resolve_js_import("./later", tmp_path, source_dir) is None

    graph._resolve_js_import.cache_clear()

    assert graph._resolve_js_import("./later", tmp_path, source_dir) == "web/src/later.ts"
