from __future__ import annotations

import json

from app.services.code_brain import pattern_miner


def test_diff_files_reuses_compiled_path_regexes(monkeypatch) -> None:
    calls = 0

    def fail_re_search(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("_diff_files should use precompiled regexes")

    monkeypatch.setattr(pattern_miner.re, "search", fail_re_search)

    diffs_json = json.dumps(
        [
            "diff --git a/app/foo.py b/app/foo.py\n--- a/app/foo.py\n+++ b/app/foo.py\n@@\n",
            "diff --git a/docs/old.md b/docs/old.md\n--- a/docs/old.md\n@@\n",
            {"not": "a diff"},
        ]
    )

    assert pattern_miner._diff_files(diffs_json) == ["app/foo.py", "docs/old.md"]
    assert calls == 0


def test_file_path_to_glob_caches_repeated_paths() -> None:
    pattern_miner._file_path_to_glob.cache_clear()

    assert pattern_miner._file_path_to_glob("app/services/code_brain/pattern_miner.py") == "app/services/**/*.py"
    assert pattern_miner._file_path_to_glob("app/services/code_brain/pattern_miner.py") == "app/services/**/*.py"

    info = pattern_miner._file_path_to_glob.cache_info()
    assert info.hits == 1
    assert info.maxsize == 4096
