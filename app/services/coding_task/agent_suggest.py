"""Task-first implementation bridge v1: bounded prompt + Code Agent, schema-free."""
from __future__ import annotations

from sqlalchemy.orm import Session

from ...models import PlanTask
from .envelope import truncate_text
from .service import build_handoff_dict
from .workspaces import get_bound_workspace_repo_for_profile, workspace_binding_reason

# Prompt bounds (allowlist-only context; no full clarifications / artifacts / raw logs)
_BRIDGE_TITLE_MAX_BYTES = 1500
_BRIDGE_BRIEF_MAX_BYTES = 12_000
_BRIDGE_BLOCKER_MAX = 3
_BRIDGE_EXTRA_MAX_BYTES = 4000
def build_bounded_implementation_prompt(
    handoff: dict,
    extra_instructions: str | None = None,
) -> str:
    """
    Strict allowlist: task id/title, brief body, sub_path, readiness summary,
    up to N blocker summaries, one-line validation status. No clarifications,
    no artifact previews, no raw validation logs.
    """
    task = handoff.get("task") or {}
    tid = task.get("id")
    title = task.get("title") or ""
    title_t, _ = truncate_text(str(title), _BRIDGE_TITLE_MAX_BYTES)
    pid = task.get("project_id")

    prof = handoff.get("profile") or {}
    sub_path = (prof.get("sub_path") or "").strip()
    code_repo_id = prof.get("code_repo_id")
    repo_name = prof.get("repo_name") or ""
    repo_path = prof.get("repo_path") or ""
    workspace_bound = bool(prof.get("workspace_bound"))

    brief = handoff.get("brief") or {}
    body = brief.get("body") or ""
    body_t, _ = truncate_text(str(body), _BRIDGE_BRIEF_MAX_BYTES)

    rc = handoff.get("readiness_context") or {}
    rstate = rc.get("coding_readiness_state", "")
    ropen = rc.get("open_clarification_count", "")

    lines: list[str] = [
        "You are assisting with a planner-tracked implementation task in CHILI.",
        "Propose changes as unified diffs only. Do not claim to have applied changes.",
        "Stay within the registered repository; respect the focus path below.",
        "",
        "## Task",
        f"- task_id: {tid}",
        f"- project_id: {pid}",
        f"- title: {title_t}",
        "",
        "## Workspace binding",
        f"- workspace_bound: {workspace_bound}",
        f"- code_repo_id: {code_repo_id}",
        f"- repo_name: {repo_name or '(unbound)'}",
        f"- repo_path: {repo_path or '(unbound)'}",
        "",
        "## Focus path (relative to repo root)",
        f"- sub_path: {sub_path or '(repo root)'}",
        "",
        "## Brief (authoritative scope text)",
        body_t if body_t else "(no brief body)",
        "",
        "## Readiness (summary only)",
        f"- coding_readiness_state: {rstate}",
        f"- open_clarification_count: {ropen}",
        "",
    ]

    val = handoff.get("validation_latest")
    if val and isinstance(val, dict):
        lines.append("## Latest validation (status only)")
        lines.append(
            f"- status: {val.get('status')} | exit_code: {val.get('exit_code')} | "
            f"timed_out: {val.get('timed_out')}"
        )
        lines.append("")

    blockers = handoff.get("blockers") or []
    if blockers and isinstance(blockers, list):
        lines.append(f"## Blockers (up to {_BRIDGE_BLOCKER_MAX} summaries)")
        for b in blockers[:_BRIDGE_BLOCKER_MAX]:
            if not isinstance(b, dict):
                continue
            sm = b.get("summary") or ""
            sm_t, _ = truncate_text(str(sm), 800)
            lines.append(f"- [{b.get('severity')}/{b.get('category')}] {sm_t}")
        lines.append("")

    if extra_instructions and str(extra_instructions).strip():
        ex_t, _ = truncate_text(str(extra_instructions).strip(), _BRIDGE_EXTRA_MAX_BYTES)
        lines.append("## Additional instructions (user-supplied)")
        lines.append(ex_t)
        lines.append("")

    lines.append("## Request")
    lines.append(
        "Analyze the codebase context you receive and propose a concrete implementation plan "
        "and diffs that satisfy the brief, respecting the focus path."
    )
    return "\n".join(lines)


async def run_agent_suggest_for_task(
    db: Session,
    task: PlanTask,
    user_id: int,
    extra_instructions: str | None = None,
) -> dict:
    handoff = build_handoff_dict(db, task, user_id=user_id)
    profile = task.coding_profile
    repo = get_bound_workspace_repo_for_profile(db, profile, user_id=user_id)
    if repo is None:
        reason = workspace_binding_reason(db, profile, user_id=user_id) or (
            "No active registered workspace matches this task profile. Bind the task to a Project workspace "
            "with code_repo_id or a registered legacy repo_index before running agent suggest."
        )
        return {
            "error": reason,
            "workspace_unbound": True,
            "workspace_reason": reason,
        }

    prompt = build_bounded_implementation_prompt(handoff, extra_instructions)
    from ..code_brain.agent import run_code_agent

    return await run_code_agent(db, prompt, repo_id=int(repo.id), user_id=user_id)
