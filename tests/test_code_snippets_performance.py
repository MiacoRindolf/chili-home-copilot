from collections import OrderedDict

from app.services.trading.brain_neural_mesh import code_snippets


class _NoSnapshotOrderedDict(OrderedDict):
    def items(self):
        raise AssertionError("snippet cache eviction should use oldest-entry pop")


def test_snippet_cache_hit_refreshes_recency(monkeypatch) -> None:
    cache = OrderedDict(
        [
            ((code_snippets._SNIPPET_CACHE_VERSION, "a"), "A"),
            ((code_snippets._SNIPPET_CACHE_VERSION, "b"), "B"),
        ]
    )
    monkeypatch.setattr(code_snippets, "_SNIPPET_CACHE", cache)

    assert code_snippets._snippet_cache_get((code_snippets._SNIPPET_CACHE_VERSION, "a")) == "A"

    assert list(cache) == [
        (code_snippets._SNIPPET_CACHE_VERSION, "b"),
        (code_snippets._SNIPPET_CACHE_VERSION, "a"),
    ]


def test_snippet_cache_set_caps_oldest_without_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(code_snippets, "_SNIPPET_CACHE_MAX", 2)
    cache = _NoSnapshotOrderedDict(
        [
            ((code_snippets._SNIPPET_CACHE_VERSION, "a"), "A"),
            ((code_snippets._SNIPPET_CACHE_VERSION, "b"), "B"),
        ]
    )
    monkeypatch.setattr(code_snippets, "_SNIPPET_CACHE", cache)

    code_snippets._snippet_cache_set((code_snippets._SNIPPET_CACHE_VERSION, "c"), "C")

    assert list(cache) == [
        (code_snippets._SNIPPET_CACHE_VERSION, "b"),
        (code_snippets._SNIPPET_CACHE_VERSION, "c"),
    ]


def test_build_code_snippet_uses_bounded_cache_for_unresolved_refs(monkeypatch) -> None:
    monkeypatch.setattr(code_snippets, "_SNIPPET_CACHE_MAX", 2)
    code_snippets._SNIPPET_CACHE.clear()

    for ref in ("missing_symbol_a", "missing_symbol_b", "missing_symbol_c"):
        assert "could not load" in code_snippets.build_code_snippet_from_ref(ref)

    assert list(code_snippets._SNIPPET_CACHE) == [
        (code_snippets._SNIPPET_CACHE_VERSION, "missing_symbol_b"),
        (code_snippets._SNIPPET_CACHE_VERSION, "missing_symbol_c"),
    ]
