"""Bridge prompt: task description is the scope fallback when no brief exists.

Live (task 36 attempt 5): the prompt carried only the title — the exact
target path lived in the unsent description — so the model invented a path.
"""

from __future__ import annotations

from app.services.coding_task.agent_suggest import build_bounded_implementation_prompt


def _handoff(brief_body: str | None, description: str = "") -> dict:
    return {
        "task": {"id": 36, "project_id": 1, "title": "Update stale tier-4 line",
                 "description": description},
        "brief": {"body": brief_body} if brief_body is not None else {},
        "profile": {"sub_path": "", "code_repo_id": 8, "repo_name": "r",
                    "repo_path": "/workspace", "workspace_bound": True},
        "readiness_context": {"coding_readiness_state": "brief_ready",
                              "open_clarification_count": 0},
    }


def test_description_used_when_no_brief():
    p = build_bounded_implementation_prompt(
        _handoff(None, description="In app/services/code_dispatch/scorer.py, fix tier 4.")
    )
    assert "app/services/code_dispatch/scorer.py" in p
    assert "(no brief body)" not in p


def test_brief_wins_over_description():
    p = build_bounded_implementation_prompt(
        _handoff("THE BRIEF SCOPE", description="ignored description")
    )
    assert "THE BRIEF SCOPE" in p
    assert "ignored description" not in p


def test_no_brief_no_description_keeps_placeholder():
    p = build_bounded_implementation_prompt(_handoff(None, description=""))
    assert "(no brief body)" in p
