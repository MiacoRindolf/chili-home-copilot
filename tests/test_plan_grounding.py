"""Plan grounding: explicit paths in the task text pin the plan.

Live (task 36 attempt 4): the task named app/services/code_dispatch/scorer.py
but qwen-7B's plan targeted a similar file from its context, burning a
17-minute run editing the wrong file. Operator-named existing paths are the
strongest grounding signal a plan can get.
"""

from __future__ import annotations

from app.services.code_brain.agent import _existing_paths_in_text


def _tree(tmp_path, *files):
    for f in files:
        p = tmp_path / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n", encoding="utf-8")


def test_extracts_existing_path_from_task_text(tmp_path):
    _tree(tmp_path, "app/services/code_dispatch/scorer.py")
    text = "In app/services/code_dispatch/scorer.py, the docstring lists LLM tiers."
    assert _existing_paths_in_text(str(tmp_path), text) == [
        "app/services/code_dispatch/scorer.py"
    ]


def test_nonexistent_and_bare_filenames_ignored(tmp_path):
    _tree(tmp_path, "app/real.py")
    text = "Touch app/ghost.py and also scorer.py please, plus app/real.py."
    assert _existing_paths_in_text(str(tmp_path), text) == ["app/real.py"]


def test_dedupes_and_handles_backslashes(tmp_path):
    _tree(tmp_path, "app/x.py")
    text = r"Edit app\x.py and app/x.py twice."
    assert _existing_paths_in_text(str(tmp_path), text) == ["app/x.py"]


def test_no_paths_returns_empty(tmp_path):
    assert _existing_paths_in_text(str(tmp_path), "make everything better") == []
