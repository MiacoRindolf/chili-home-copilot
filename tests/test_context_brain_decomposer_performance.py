from __future__ import annotations

from app.services.context_brain import decomposer


def test_heuristic_sentence_split_uses_precompiled_regex(monkeypatch) -> None:
    calls = 0

    def fail_re_split(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("_heuristic_should_decompose should use precompiled split regex")

    monkeypatch.setattr(decomposer.re, "split", fail_re_split)

    query = (
        "Please inspect the trading cache behavior and explain the main bottleneck. "
        "Then compare it with the broker retry path and identify the safest fix. "
        "Finally summarize the benchmark evidence and any residual risk."
    )

    assert decomposer._heuristic_should_decompose(query) is True
    assert calls == 0


def test_parse_decomposer_json_uses_precompiled_fence_regexes(monkeypatch) -> None:
    calls = 0

    def fail_re_sub(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("_parse_decomposer_json should use precompiled fence regexes")

    monkeypatch.setattr(decomposer.re, "sub", fail_re_sub)

    parsed = decomposer._parse_decomposer_json(
        '```json\n{"decompose": false, "chunks": []}\n```'
    )

    assert parsed == {"decompose": False, "chunks": []}
    assert calls == 0


def test_parse_decomposer_json_fast_paths_clean_json() -> None:
    calls = 0

    class _FindTrackingJson(str):
        def strip(self):
            return self

        def find(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise AssertionError("clean JSON should parse before balanced-brace scanning")

    parsed = decomposer._parse_decomposer_json(_FindTrackingJson('{"decompose": false, "chunks": []}'))

    assert parsed == {"decompose": False, "chunks": []}
    assert calls == 0
