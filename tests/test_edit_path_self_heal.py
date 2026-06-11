"""Planner path self-healing — the task-36 live failure.

qwen-7B planned app/services/scorer.py for app/services/code_dispatch/
scorer.py (dropped a segment) and the edit step refused with File-not-found.
_resolve_planned_path heals unique-basename slips and never guesses on
ambiguity.
"""

from __future__ import annotations

from app.services.code_brain.agent import _resolve_planned_path


def _tree(tmp_path, *files):
    for f in files:
        p = tmp_path / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n", encoding="utf-8")


def test_heals_dropped_directory_segment(tmp_path):
    _tree(tmp_path, "app/services/code_dispatch/scorer.py")
    assert (
        _resolve_planned_path(str(tmp_path), "app/services/scorer.py")
        == "app/services/code_dispatch/scorer.py"
    )


def test_exact_path_passes_through(tmp_path):
    _tree(tmp_path, "app/services/code_dispatch/scorer.py")
    assert (
        _resolve_planned_path(str(tmp_path), "app/services/code_dispatch/scorer.py")
        == "app/services/code_dispatch/scorer.py"
    )


def test_ambiguous_basename_returns_none(tmp_path):
    _tree(tmp_path, "a/util.py", "b/util.py")
    assert _resolve_planned_path(str(tmp_path), "x/util.py") is None


def test_tail_match_disambiguates(tmp_path):
    _tree(tmp_path, "a/scorer.py", "code_dispatch/scorer.py")
    assert (
        _resolve_planned_path(str(tmp_path), "wrong/code_dispatch/scorer.py")
        == "code_dispatch/scorer.py"
    )


def test_missing_everywhere_returns_none(tmp_path):
    _tree(tmp_path, "a/x.py")
    assert _resolve_planned_path(str(tmp_path), "a/nope.py") is None
