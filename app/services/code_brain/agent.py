"""Code Agent: analyze request -> gather context -> plan -> edit -> validate.

Uses a two-step LLM flow (plan-then-edit) so every diff is generated with
the real file contents in context, eliminating placeholder/hallucinated code.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ...models.code_brain import CodeHotspot, CodeInsight, CodeRepo, CodeSnapshot
from . import insights as insights_mod
from .indexer import get_accessible_repo_ids, get_accessible_repos
from .runtime import resolve_repo_runtime_path
from .search import search_code

logger = logging.getLogger(__name__)

_MAX_FILE_LINES = 600
_MAX_FILES_PER_EDIT = 8


def _gather_context(
    db: Session,
    repo_id: Optional[int],
    prompt: str,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Build rich context from the Code Brain for the LLM."""
    context: Dict[str, Any] = {
        "repos": [],
        "insights": [],
        "hotspots": [],
        "relevant_files": [],
    }

    if repo_id:
        repos = []
        for repo in get_accessible_repos(db, user_id=user_id, include_shared=True):
            if int(repo.id) == int(repo_id):
                repos = [repo]
                break
    else:
        repos = get_accessible_repos(db, user_id=user_id, include_shared=True)

    for repo in repos:
        lang_stats = json.loads(repo.language_stats) if repo.language_stats else {}
        context["repos"].append({
            "id": repo.id,
            "name": repo.name,
            "path": repo.host_path or repo.path,
            "runtime_path": str(resolve_repo_runtime_path(repo) or ""),
            "file_count": repo.file_count,
            "total_lines": repo.total_lines,
            "languages": lang_stats,
            "frameworks": repo.framework_tags.split(",") if repo.framework_tags else [],
        })

    repo_ids = [int(repo.id) for repo in repos]
    all_insights = insights_mod.get_insights(db, repo_id=repo_id, repo_ids=repo_ids if repo_id is None else None)
    context["insights"] = all_insights[:30]

    for repo in repos:
        hotspots = (
            db.query(CodeHotspot)
            .filter(CodeHotspot.repo_id == repo.id)
            .order_by(CodeHotspot.combined_score.desc())
            .limit(10)
            .all()
        )
        for h in hotspots:
            context["hotspots"].append({
                "file": h.file_path,
                "repo": repo.name,
                "churn": round(h.churn_score, 3),
                "complexity": round(h.complexity_score, 3),
            })

    # Use semantic search for relevance instead of naive path-word matching
    for repo in repos:
        try:
            results = search_code(db, prompt, repo_id=repo.id, limit=20)
            seen: set[str] = set()
            for r in results:
                fp = r["file"]
                if fp in seen:
                    continue
                seen.add(fp)
                context["relevant_files"].append({
                    "file": fp,
                    "repo": repo.name,
                    "repo_path": str(resolve_repo_runtime_path(repo) or repo.host_path or repo.path),
                    "language": None,
                    "lines": 0,
                    "complexity": 0,
                    "relevance": r.get("score", 0.5),
                    "symbol": r.get("symbol", ""),
                })
        except Exception:
            pass

    # Fallback: if search returned nothing, use path-word matching
    if not context["relevant_files"]:
        prompt_lower = prompt.lower()
        for repo in repos:
            snaps = db.query(CodeSnapshot).filter(CodeSnapshot.repo_id == repo.id).all()
            for snap in snaps:
                file_lower = snap.file_path.lower()
                relevance = sum(1 for word in prompt_lower.split() if len(word) > 2 and word in file_lower)
                if relevance > 0:
                    context["relevant_files"].append({
                        "file": snap.file_path,
                        "repo": repo.name,
                        "repo_path": str(resolve_repo_runtime_path(repo) or repo.host_path or repo.path),
                        "language": snap.language,
                        "lines": snap.line_count,
                        "complexity": snap.complexity_score,
                        "relevance": relevance,
                    })
        context["relevant_files"].sort(key=lambda x: -x["relevance"])
        context["relevant_files"] = context["relevant_files"][:20]

    return context


def _build_plan_prompt(context: Dict[str, Any]) -> str:
    """System prompt for Step 1: produce a structured plan only, no diffs."""
    parts = [
        "You are Chili Code Agent, an expert software engineer with deep knowledge of the user's codebases.",
        "You have been continuously learning the user's projects.",
        "",
    ]

    if context["repos"]:
        parts.append("## Registered Repositories")
        for r in context["repos"]:
            langs = ", ".join(f"{k}: {v}" for k, v in sorted(r["languages"].items(), key=lambda x: -x[1])[:5])
            fws = ", ".join(r["frameworks"]) if r["frameworks"] else "none detected"
            parts.append(f"- **{r['name']}** ({r['path']}): {r['file_count']} files, {r['total_lines']} lines | Languages: {langs} | Frameworks: {fws}")
        parts.append("")

    if context["insights"]:
        parts.append("## Discovered Patterns & Conventions")
        for ins in context["insights"][:15]:
            parts.append(f"- [{ins['category']}] {ins['description']} (confidence: {ins['confidence']:.0%})")
        parts.append("")

    if context["hotspots"]:
        parts.append("## Code Hotspots (high churn + complexity)")
        for h in context["hotspots"][:10]:
            parts.append(f"- {h['file']} (churn: {h['churn']}, complexity: {h['complexity']})")
        parts.append("")

    if context["relevant_files"]:
        parts.append("## Potentially Relevant Files")
        for f in context["relevant_files"][:15]:
            sym = f" (contains: {f['symbol']})" if f.get("symbol") else ""
            parts.append(f"- {f['file']}{sym}")
        parts.append("")

    parts.extend([
        "## Your Task",
        "Analyze the user's request and produce ONLY a structured plan.",
        "Do NOT generate any code or diffs yet.",
        "",
        "## Required Output Format",
        "Return a JSON object with this exact structure:",
        "```json",
        '{',
        '  "analysis": "one paragraph understanding of the request",',
        '  "files": [',
        '    {"path": "relative/file/path.py", "action": "modify|create", "description": "what to change and why"}',
        '  ],',
        '  "notes": "any caveats or alternative approaches"',
        '}',
        "```",
        "",
        "RULES:",
        "- Only include files that actually exist in the repository (listed above) or new files to create.",
        "- Be specific about what needs to change in each file.",
        "- Limit to the most important files (max 8).",
    ])

    return "\n".join(parts)


def _build_edit_prompt(file_path: str, file_content: str, change_description: str, conventions: List[str]) -> str:
    """System prompt for Step 2: generate a diff for one specific file."""
    parts = [
        "You are Chili Code Agent. You are editing a specific file.",
        "",
        "STRICT RULES:",
        "- Your diff MUST be based ONLY on the file content provided below.",
        "- Every line you mark with '-' (removal) MUST exist verbatim in the current file.",
        "- Do NOT invent or guess code that is not in the provided file.",
        "- Do NOT use placeholder code like '# Implementation here' or 'pass' for real logic.",
        "- If you cannot accomplish the change with the provided content, explain why instead of guessing.",
        "- Use proper unified diff format with --- a/ and +++ b/ headers.",
        "",
    ]

    if conventions:
        parts.append("## Project Conventions to Follow")
        for c in conventions[:5]:
            parts.append(f"- {c}")
        parts.append("")

    parts.extend([
        f"## File: {file_path}",
        "```",
        file_content,
        "```",
        "",
        "## Required Change",
        change_description,
        "",
        "## Output",
        "Return ONLY a unified diff block wrapped in ```diff ... ```. No other text.",
    ])

    return "\n".join(parts)


def _read_file_content(repo_path: str, file_path: str, max_lines: int = _MAX_FILE_LINES) -> Optional[str]:
    """Read file content for LLM context. Returns None if unreadable."""
    try:
        full = Path(repo_path) / file_path
        if not full.is_file():
            return None
        lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) > max_lines:
            return "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} more lines truncated)"
        return "\n".join(lines)
    except Exception:
        return None


def _parse_plan_json(reply: str) -> Optional[Dict[str, Any]]:
    """Extract the JSON plan from the LLM's Step 1 response."""
    # Try to find JSON block
    m = re.search(r"```(?:json)?\s*\n(\{.*?\})\s*\n```", reply, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Try raw JSON
    m = re.search(r"\{[^{}]*\"files\"[^{}]*\[.*?\].*?\}", reply, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


def _validate_diff(diff_text: str, file_path: str, file_content: Optional[str]) -> Dict[str, Any]:
    """Validate a generated diff against the real file content."""
    result = {"valid": True, "warnings": []}

    if not file_content:
        result["warnings"].append(f"Cannot validate: file '{file_path}' not readable")
        return result

    real_lines = set(file_content.splitlines())
    removed_lines = []
    for line in diff_text.splitlines():
        if line.startswith("-") and not line.startswith("---"):
            removed_lines.append(line[1:])

    bad_count = 0
    for rl in removed_lines:
        stripped = rl.strip()
        if stripped and stripped not in {l.strip() for l in real_lines}:
            bad_count += 1

    if bad_count > 0 and removed_lines:
        pct = bad_count / len(removed_lines) * 100
        if pct > 50:
            result["valid"] = False
            result["warnings"].append(
                f"{bad_count}/{len(removed_lines)} removed lines do not match the actual file. "
                "This diff may contain hallucinated code."
            )
        elif pct > 20:
            result["warnings"].append(
                f"{bad_count}/{len(removed_lines)} removed lines could not be verified."
            )

    return result


async def run_code_agent(
    db: Session,
    prompt: str,
    repo_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Execute the two-step Code Agent flow: Plan -> Edit.

    Phase F.10: every LLM call here goes through the Universal Gateway
    with a specific purpose tag so the brain can apply the right
    routing strategy per call site (plan = tree-decomposed, edit/create
    = augmented). Falls back to direct openai_client.chat on any error.
    """
    from ...openai_client import is_configured

    if not is_configured():
        return {"error": "LLM not configured. Set LLM_API_KEY or PREMIUM_API_KEY in .env"}

    # Local helper so the three call sites below stay tidy. Each call
    # gets a different purpose so the policy table can route them
    # individually.
    from ..context_brain.llm_gateway import gateway_chat as _gateway_chat
    from ...openai_client import chat as _legacy_chat

    def llm_chat(messages, system_prompt=None, trace_id="llm",
                 user_message="", max_tokens=1024, strict_escalation=True,
                 _purpose="code_dispatch_plan"):
        try:
            return _gateway_chat(
                messages=messages,
                purpose=_purpose,
                system_prompt=system_prompt,
                trace_id=trace_id,
                user_message=user_message,
                max_tokens=max_tokens,
                strict_escalation=strict_escalation,
                user_id=user_id,
                db=db,
            )
        except Exception as _e:
            logger.warning("[code_agent] gateway_chat failed (%s); falling back", _e)
            return _legacy_chat(
                messages=messages,
                system_prompt=system_prompt,
                trace_id=trace_id,
                user_message=user_message,
                max_tokens=max_tokens,
                strict_escalation=strict_escalation,
            )

    # Track gateway calls so we can record outcomes against them at the end.
    _dispatch_log_ids: list[tuple[int, str, bool]] = []  # (gateway_log_id, purpose, success)

    context = _gather_context(db, repo_id, prompt, user_id=user_id)

    if not context["repos"]:
        return {"error": "No repos registered. Add a repo first via the Brain UI."}

    # ── Step 1: Plan ─────────────────────────────────────────────
    plan_system = _build_plan_prompt(context)
    plan_result = llm_chat(
        messages=[{"role": "user", "content": prompt}],
        system_prompt=plan_system,
        trace_id="code-agent-plan",
        user_message=prompt,
        max_tokens=1500,
    )
    plan_reply = plan_result.get("reply", "")
    plan_model = plan_result.get("model", "unknown")

    # ── DO NOT REMOVE — masking-bug guard (CHILI Dispatch Phase D.0) ──
    # openai_client.chat() returns {"reply": "", "model": "error"} when every
    # cascade tier failed. Without this check, the empty sentinel was being
    # persisted as a fake success-shaped row in coding_agent_suggestion
    # (model='error', diffs=[]), masking real failures from the dispatch
    # loop and polluting distillation training data.
    if plan_model == "error" or not plan_reply.strip():
        logger.warning(
            "[code_agent] LLM cascade returned empty/error sentinel "
            "(model=%s, reply_len=%d) — surfacing as error",
            plan_model, len(plan_reply),
        )
        return {
            "error": (
                "LLM cascade exhausted: every configured tier (OpenAI / Groq / Gemini) "
                "returned an empty response. Check provider keys, daily quotas, and "
                "auth-failed sticky markers in the scheduler-worker logs."
            ),
            "model": plan_model,
        }

    plan_json = _parse_plan_json(plan_reply)

    # Record the plan's gateway_log_id (its outcome quality is set at the end).
    _plan_log_id = plan_result.get("gateway_log_id") if isinstance(plan_result, dict) else None
    if _plan_log_id:
        _dispatch_log_ids.append((int(_plan_log_id), "code_dispatch_plan", False))

    if not plan_json or not plan_json.get("files"):
        # Plan didn't produce a usable plan — still a soft failure.
        try:
            from ..context_brain.outcome_tracker import record_dispatch_outcome
            for log_id, purpose, _ in _dispatch_log_ids:
                record_dispatch_outcome(
                    db, gateway_log_id=log_id, success=False,
                    purpose=purpose,
                    detail={"reason": "plan_no_files", "files_changed": 0},
                )
        except Exception:
            pass
        return {
            "response": plan_reply,
            "model": plan_model,
            "diffs": [],
            "files_changed": [],
            "validation": [],
            "context_used": _context_summary(context, 0),
        }

    analysis = plan_json.get("analysis", "")
    plan_files = plan_json.get("files", [])[:_MAX_FILES_PER_EDIT]
    notes = plan_json.get("notes", "")

    # Collect conventions for the edit prompts
    conventions = [
        ins["description"] for ins in context["insights"]
        if ins.get("category") in ("convention", "pattern")
    ][:5]

    # Resolve repo paths
    repo_path_map: Dict[str, str] = {}
    for r in context["repos"]:
        repo_path_map[r["name"]] = r["path"]
    default_repo_path = context["repos"][0]["path"] if context["repos"] else ""

    # ── Step 2: Edit each file ───────────────────────────────────
    all_diffs: List[str] = []
    files_changed: List[str] = []
    validations: List[Dict[str, Any]] = []
    edit_sections: List[str] = []

    for pf in plan_files:
        fpath = pf.get("path", "")
        action = pf.get("action", "modify")
        description = pf.get("description", "")

        if action == "create":
            edit_sections.append(f"### New file: {fpath}\n{description}")
            # For new files, ask LLM to generate the full file content
            create_prompt = (
                f"You are creating a new file: {fpath}\n\n"
                f"Requirements: {description}\n\n"
                "Follow the project conventions:\n" +
                "\n".join(f"- {c}" for c in conventions[:3]) +
                "\n\nReturn ONLY the file content wrapped in a code block."
            )
            create_result = llm_chat(
                messages=[{"role": "user", "content": create_prompt}],
                system_prompt="You are Chili Code Agent. Generate clean, production-quality code.",
                trace_id=f"code-agent-create-{fpath}",
                user_message=description,
                max_tokens=3000,
                _purpose="code_dispatch_create",
            )
            create_reply = create_result.get("reply", "")
            _create_log_id = create_result.get("gateway_log_id") if isinstance(create_result, dict) else None
            m = re.search(r"```\w*\n(.*?)```", create_reply, re.DOTALL)
            if m:
                new_content = m.group(1).strip()
                diff = f"--- /dev/null\n+++ b/{fpath}\n@@ -0,0 +1,{len(new_content.splitlines())} @@\n"
                diff += "\n".join("+" + l for l in new_content.splitlines())
                all_diffs.append(diff)
                files_changed.append(fpath)
                validations.append({"file": fpath, "valid": True, "warnings": ["New file"]})
                if _create_log_id:
                    _dispatch_log_ids.append((int(_create_log_id), "code_dispatch_create", True))
            else:
                edit_sections.append(f"Could not generate content for {fpath}")
                if _create_log_id:
                    _dispatch_log_ids.append((int(_create_log_id), "code_dispatch_create", False))
            continue

        file_content = _read_file_content(default_repo_path, fpath)
        if file_content is None:
            # Try other repos
            for rp in repo_path_map.values():
                file_content = _read_file_content(rp, fpath)
                if file_content is not None:
                    break

        if file_content is None:
            validations.append({
                "file": fpath,
                "valid": False,
                "warnings": [f"File not found in any registered repo: {fpath}"],
            })
            edit_sections.append(f"### {fpath}\nFile not found -- skipped.")
            continue

        edit_system = _build_edit_prompt(fpath, file_content, description, conventions)
        edit_result = llm_chat(
            messages=[{"role": "user", "content": f"Apply the change to {fpath} as described."}],
            system_prompt=edit_system,
            trace_id=f"code-agent-edit-{fpath}",
            user_message=description,
            max_tokens=3000,
            _purpose="code_dispatch_edit",
        )
        edit_reply = edit_result.get("reply", "")
        _edit_log_id = edit_result.get("gateway_log_id") if isinstance(edit_result, dict) else None

        diffs_in_reply = re.findall(r"```diff\n(.*?)```", edit_reply, re.DOTALL)
        _edit_succeeded_at_least_one = False
        if diffs_in_reply:
            for d in diffs_in_reply:
                validation = _validate_diff(d, fpath, file_content)
                validations.append({"file": fpath, **validation})
                if validation["valid"]:
                    all_diffs.append(d)
                    if fpath not in files_changed:
                        files_changed.append(fpath)
                    edit_sections.append(f"### {fpath}\n```diff\n{d}\n```")
                    _edit_succeeded_at_least_one = True
                else:
                    edit_sections.append(
                        f"### {fpath}\n**Diff rejected** -- {'; '.join(validation['warnings'])}"
                    )
        else:
            edit_sections.append(f"### {fpath}\n{edit_reply}")
        if _edit_log_id:
            _dispatch_log_ids.append(
                (int(_edit_log_id), "code_dispatch_edit", _edit_succeeded_at_least_one)
            )

    # ── Assemble final response ──────────────────────────────────
    response_parts = [f"## Analysis\n{analysis}"]
    response_parts.append(
        "## Plan\n" +
        "\n".join(f"- **{pf['path']}** ({pf.get('action','modify')}): {pf.get('description','')}" for pf in plan_files)
    )
    if edit_sections:
        response_parts.append("## Changes\n" + "\n\n".join(edit_sections))
    if notes:
        response_parts.append(f"## Notes\n{notes}")

    # Add validation summary
    invalid = [v for v in validations if not v.get("valid")]
    if invalid:
        warn_lines = [f"- **{v['file']}**: {'; '.join(v.get('warnings', []))}" for v in invalid]
        response_parts.append("## Validation Warnings\n" + "\n".join(warn_lines))

    # F.5 — record dispatch outcomes against every gateway call we made.
    # Plan succeeds when at least one file was actually changed; create/edit
    # entries already carry their own per-file success bit.
    try:
        from ..context_brain.outcome_tracker import record_dispatch_outcome
        plan_success = bool(files_changed)
        invalid_count = sum(1 for v in validations if not v.get("valid"))
        for log_id, purpose, per_file_success in _dispatch_log_ids:
            ok = per_file_success if purpose != "code_dispatch_plan" else plan_success
            record_dispatch_outcome(
                db,
                gateway_log_id=log_id,
                success=ok,
                purpose=purpose,
                detail={
                    "files_changed": len(files_changed),
                    "files_planned": len(plan_files),
                    "invalid_diffs": invalid_count,
                },
            )
    except Exception as _e:  # pragma: no cover
        logger.warning("[code_agent] failed to record dispatch outcomes: %s", _e)

    return {
        "response": "\n\n".join(response_parts),
        "model": plan_result.get("model", "unknown"),
        "diffs": all_diffs,
        "files_changed": files_changed,
        "validation": validations,
        "context_used": _context_summary(context, len(plan_files)),
    }


def _context_summary(context: Dict[str, Any], files_edited: int) -> Dict[str, Any]:
    return {
        "repos": len(context["repos"]),
        "insights": len(context["insights"]),
        "hotspots": len(context["hotspots"]),
        "relevant_files": len(context["relevant_files"]),
        "files_in_plan": files_edited,
    }
