"""Task-first implementation bridge v1: bounded prompt + Code Agent, schema-free."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from ...models import PlanTask
from ...models.code_brain import CodeRepo
from .envelope import list_code_repo_roots, truncate_text
from .service import build_handoff_dict

# Prompt bounds (allowlist-only context; no full clarifications / artifacts / raw logs)
_BRIDGE_TITLE_MAX_BYTES = 1500
_BRIDGE_BRIEF_MAX_BYTES = 12_000
_BRIDGE_BLOCKER_MAX = 3
_BRIDGE_EXTRA_MAX_BYTES = 4000


def resolve_code_repo_id_for_task_repo_index(db: Session, user_id: int, repo_index: int) -> int | None:
    """
    Map task profile repo_index to CodeRepo.id by matching resolved paths.
    Fail closed: no guess, no fallback to another index.
    """
    roots = list_code_repo_roots()
    if not roots or repo_index < 0 or repo_index >= len(roots):
        return None
    try:
        target = roots[repo_index].resolve()
    except OSError:
        return None
    rows = (
        db.query(CodeRepo)
        .filter(CodeRepo.active.is_(True), CodeRepo.user_id == user_id)
        .all()
    )
    for r in rows:
        try:
            if Path(r.path).resolve() == target:
                return int(r.id)
        except OSError:
            continue
    return None


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
    handoff = build_handoff_dict(db, task)
    profile = handoff.get("profile") or {}
    try:
        ri = int(profile.get("repo_index", 0))
    except (TypeError, ValueError):
        ri = 0

    repo_id = resolve_code_repo_id_for_task_repo_index(db, user_id, ri)
    if repo_id is None:
        return {
            "error": (
                "No active Code Brain repository matches this task's coding profile (repo_index). "
                "Register an indexed repo whose path matches CHILI's code_brain_repos entry for "
                "that index, under your user, or adjust the task profile."
            )
        }

    prompt = build_bounded_implementation_prompt(handoff, extra_instructions)
    from ..code_brain.agent import run_code_agent

    return await run_code_agent(db, prompt, repo_id=repo_id, user_id=user_id)
