"""SEARCH/REPLACE edit mechanics (small-model-friendly editing).

Small local models emit malformed unified diffs constantly; exact-match
search/replace blocks are reliable, and the diff handed to downstream
``git apply`` is generated programmatically — always well-formed. The
exact-match + uniqueness rule IS the anti-hallucination check.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from app.config import settings
from app.services.code_brain.agent import (
    _apply_search_replace,
    _build_edit_prompt,
    _elide_for_prompt,
    _parse_search_replace_blocks,
    _unified_diff_text,
)


FILE = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"


def _block(search: str, replace: str) -> str:
    return f"<<<<<<< SEARCH\n{search}\n=======\n{replace}\n>>>>>>> REPLACE"


# ── parsing ──────────────────────────────────────────────────────────────


def test_parse_single_and_multiple_blocks():
    reply = _block("a", "b")
    assert _parse_search_replace_blocks(reply) == [("a", "b")]

    reply2 = _block("one", "uno") + "\nsome chatter\n" + _block("two", "dos")
    assert _parse_search_replace_blocks(reply2) == [("one", "uno"), ("two", "dos")]


def test_parse_tolerates_crlf_and_returns_empty_on_prose():
    reply = "<<<<<<< SEARCH\r\nold\r\n=======\r\nnew\r\n>>>>>>> REPLACE"
    assert _parse_search_replace_blocks(reply) == [("old", "new")]
    assert _parse_search_replace_blocks("I cannot make this change because ...") == []


# ── applying ─────────────────────────────────────────────────────────────


def test_apply_unique_match():
    out = _apply_search_replace(FILE, [("    return a + b", "    return a + b  # checked")])
    assert out["applied"] == 1
    assert "# checked" in out["new_content"]
    assert out["warnings"] == []


def test_apply_rejects_hallucinated_search():
    out = _apply_search_replace(FILE, [("def mul(a, b):", "def mul(a, b):  # nope")])
    assert out["applied"] == 0
    assert out["new_content"] is None
    assert "not found" in out["warnings"][0]


def test_apply_rejects_ambiguous_search():
    out = _apply_search_replace(FILE, [("def ", "class ")])
    assert out["applied"] == 0
    assert "matches 2 times" in out["warnings"][0]


def test_apply_insertion_via_anchor_repetition():
    out = _apply_search_replace(
        FILE,
        [(
            "def sub(a, b):\n    return a - b",
            "def sub(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b",
        )],
    )
    assert out["applied"] == 1
    assert "def mul" in out["new_content"]


def test_apply_multiple_blocks_partial_success():
    out = _apply_search_replace(
        FILE,
        [
            ("def add(a, b):", "def add(a: int, b: int) -> int:"),
            ("nonexistent", "whatever"),
        ],
    )
    assert out["applied"] == 1
    assert "a: int" in out["new_content"]
    assert len(out["warnings"]) == 1


# ── machine-generated diff is git-apply compatible (the load-bearing bit) ─


def test_unified_diff_round_trips_through_git_apply(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    target = tmp_path / "mod.py"
    target.write_text(FILE, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)

    out = _apply_search_replace(FILE, [("    return a - b", "    return a - b  # edited")])
    diff = _unified_diff_text("mod.py", FILE, out["new_content"])
    assert diff.startswith("--- a/mod.py")

    patch = tmp_path / "p.patch"
    patch.write_text(diff, encoding="utf-8")
    check = subprocess.run(
        ["git", "apply", "--check", str(patch)], cwd=tmp_path, capture_output=True, text=True,
    )
    assert check.returncode == 0, f"git apply --check failed: {check.stderr}"
    subprocess.run(["git", "apply", str(patch)], cwd=tmp_path, check=True)
    assert target.read_text(encoding="utf-8") == out["new_content"]


def test_unified_diff_handles_missing_trailing_newline(tmp_path):
    old = "x = 1"  # no trailing newline
    new = "x = 2"
    diff = _unified_diff_text("f.py", old, new)
    assert "-x = 1" in diff and "+x = 2" in diff


# ── prompt budget ────────────────────────────────────────────────────────


def test_elide_under_budget_is_identity():
    assert _elide_for_prompt(FILE) == FILE


def test_elide_over_budget_keeps_head_and_tail(monkeypatch):
    monkeypatch.setattr(settings, "chili_code_gen_max_tokens", 25)  # budget = 100 chars
    content = "HEAD" + ("x" * 500) + "TAIL"
    out = _elide_for_prompt(content)
    assert out.startswith("HEAD")
    assert out.endswith("TAIL")
    assert "elided" in out
    assert len(out) < len(content)


def test_edit_prompt_teaches_search_replace_format():
    p = _build_edit_prompt("a.py", "x = 1\n", "rename x to y", [])
    assert "<<<<<<< SEARCH" in p
    assert ">>>>>>> REPLACE" in p
    assert "EXACTLY ONCE" in p


# ── fuzzy fallback: normalized match onto the exact original span ────────


EMDASH_FILE = "\"\"\"Tiers:\n  4 — OpenAI gpt-4o or Anthropic claude-opus-4.6 (premium)\n\"\"\"\nx = 1\n"


def test_fuzzy_match_em_dash_and_whitespace():
    """The task-37 run-685 live failure: file has an em-dash, model wrote
    a hyphen. The fuzzy fallback must edit the EXACT original line."""
    out = _apply_search_replace(
        EMDASH_FILE,
        [("  4 - OpenAI gpt-4o or Anthropic claude-opus-4.6 (premium)",
          "  4 - frontier escalation (FRONTIER_MODEL, e.g. gpt-5.5)")],
    )
    assert out["applied"] == 1
    assert "frontier escalation" in out["new_content"]
    assert "gpt-4o" not in out["new_content"]
    assert any("normalization" in w for w in out["warnings"])
    assert "x = 1" in out["new_content"]


def test_fuzzy_match_requires_uniqueness():
    content = "a — one\nb\na — one\n"
    out = _apply_search_replace(content, [("a - one", "a - two")])
    assert out["applied"] == 0  # two normalized matches -> never guess


def test_fuzzy_match_ignores_blank_only_search():
    out = _apply_search_replace("x = 1\n", [("   ", "y = 2")])
    assert out["applied"] == 0


def test_fuzzy_match_reindents_replace_to_file_truth():
    """Run-687 live failure: the model copied a module docstring with +4
    indentation and its REPLACE kept it -> SyntaxError. The fuzzy path must
    re-anchor REPLACE to the file's real indentation."""
    content = "\"\"\"Tiers:\n  4 — old premium line\n\"\"\"\nx = 1\n"
    out = _apply_search_replace(
        content,
        [("    \"\"\"Tiers:\n      4 - old premium line\n    \"\"\"",
          "    \"\"\"Tiers:\n      4 - frontier escalation\n    \"\"\"")],
    )
    assert out["applied"] == 1
    nc = out["new_content"]
    assert nc.startswith("\"\"\"Tiers:")          # no leading indent on line 1
    assert "\n  4 - frontier escalation\n" in nc   # body keeps 2-space indent
    assert "    \"\"\"" not in nc


def test_reindent_noop_when_indentation_agrees():
    content = "def f():\n    x = 1\n"
    out = _apply_search_replace(content, [("    x = 1", "    x = 2")])
    assert out["applied"] == 1
    assert "    x = 2" in out["new_content"]
