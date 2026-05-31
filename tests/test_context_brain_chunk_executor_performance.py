from __future__ import annotations

from app.services.context_brain import chunk_executor


def test_similarity_exact_match_skips_sequence_matcher(monkeypatch) -> None:
    calls = 0

    class FailSequenceMatcher:
        def __init__(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise AssertionError("exact similarity should not instantiate SequenceMatcher")

    monkeypatch.setattr(chunk_executor, "SequenceMatcher", FailSequenceMatcher)

    assert chunk_executor._similarity("  same answer  ", "same answer") == 1.0
    assert calls == 0


def test_similarity_non_exact_still_uses_sequence_matcher(monkeypatch) -> None:
    calls = 0

    class FakeSequenceMatcher:
        def __init__(self, _junk, left, right):
            nonlocal calls
            calls += 1
            assert left == "alpha"
            assert right == "beta"

        def ratio(self):
            return 0.25

    monkeypatch.setattr(chunk_executor, "SequenceMatcher", FakeSequenceMatcher)

    assert chunk_executor._similarity(" alpha ", " beta ") == 0.25
    assert calls == 1
