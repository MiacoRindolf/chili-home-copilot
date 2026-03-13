"""Code Agent: analyze request -> gather context -> propose changes -> generate diffs.

Uses the Code Brain's knowledge (project structure, conventions, hotspots, insights)
combined with the configured LLM to act as an intelligent coding assistant that
deeply understands the user's codebase.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ...models.code_brain import CodeHotspot, CodeInsight, CodeRepo, CodeSnapshot
from . import insights as insights_mod
from .indexer import get_registered_repos

logger = logging.getLogger(__name__)


def _gather_context(db: Session, repo_id: Optional[int], prompt: str) -> Dict[str, Any]:
    """Build rich context from the Code Brain for the LLM."""
    context: Dict[str, Any] = {
        "repos": [],
        "insights": [],
        "hotspots": [],
        "relevant_files": [],
    }

    if repo_id:
        repo = db.query(CodeRepo).filter(CodeRepo.id == repo_id, CodeRepo.active.is_(True)).first()
        repos = [repo] if repo else []
    else:
        repos = db.query(CodeRepo).filter(CodeRepo.active.is_(True)).all()

    for repo in repos:
        lang_stats = json.loads(repo.language_stats) if repo.language_stats else {}
        context["repos"].append({
            "name": repo.name,
            "path": repo.path,
            "file_count": repo.file_count,
            "total_lines": repo.total_lines,
            "languages": lang_stats,
            "frameworks": repo.framework_tags.split(",") if repo.framework_tags else [],
        })

    all_insights = insights_mod.get_insights(db, repo_id=repo_id)
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

    prompt_lower = prompt.lower()
    for repo in repos:
        snaps = (
            db.query(CodeSnapshot)
            .filter(CodeSnapshot.repo_id == repo.id)
            .all()
        )
        for snap in snaps:
            file_lower = snap.file_path.lower()
            relevance = 0
            for word in prompt_lower.split():
                if len(word) > 2 and word in file_lower:
                    relevance += 1
            if relevance > 0:
                context["relevant_files"].append({
                    "file": snap.file_path,
                    "repo": repo.name,
                    "repo_path": repo.path,
                    "language": snap.language,
                    "lines": snap.line_count,
                    "complexity": snap.complexity_score,
                    "relevance": relevance,
                })

    context["relevant_files"].sort(key=lambda x: -x["relevance"])
    context["relevant_files"] = context["relevant_files"][:20]

    return context


def _build_system_prompt(context: Dict[str, Any]) -> str:
    """Build the system prompt with Code Brain context."""
    parts = [
        "You are Chili Code Agent, an expert software engineer with deep knowledge of the user's codebases.",
        "You have been continuously learning the user's projects and have the following understanding:",
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
        for f in context["relevant_files"][:10]:
            parts.append(f"- {f['file']} ({f['language']}, {f['lines']} lines, complexity: {f['complexity']:.1f})")
        parts.append("")

    parts.extend([
        "## Your Capabilities",
        "1. **Analyze**: Understand the user's request in context of their codebase",
        "2. **Reason**: Identify which files need changes and why, using your knowledge of the project's patterns",
        "3. **Propose**: Generate specific, well-structured code changes as unified diffs",
        "4. **Explain**: Describe your reasoning and any trade-offs",
        "",
        "## Response Format",
        "Always structure your response as:",
        "1. **Analysis**: Brief understanding of the request",
        "2. **Plan**: Which files to modify/create and why",
        "3. **Changes**: Specific code changes in unified diff format (```diff blocks)",
        "4. **Notes**: Any caveats, follow-up suggestions, or alternative approaches",
        "",
        "When proposing code, follow the project's conventions (naming, imports, patterns) that you've learned.",
        "Be precise and actionable. Don't explain obvious things.",
    ])

    return "\n".join(parts)


def _read_file_content(repo_path: str, file_path: str, max_lines: int = 500) -> Optional[str]:
    """Read file content for LLM context. Returns None if unreadable."""
    try:
        full = Path(repo_path) / file_path
        if not full.is_file():
            return None
        lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) > max_lines:
            return "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} more lines)"
        return "\n".join(lines)
    except Exception:
        return None


async def run_code_agent(
    db: Session,
    prompt: str,
    repo_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Execute the Code Agent flow."""
    from ...openai_client import chat as llm_chat, is_configured

    if not is_configured():
        return {"error": "LLM not configured. Set LLM_API_KEY or PREMIUM_API_KEY in .env"}

    context = _gather_context(db, repo_id, prompt)

    if not context["repos"]:
        return {"error": "No repos registered. Add a repo first via the Brain UI."}

    system_prompt = _build_system_prompt(context)

    file_contents = []
    for f in context["relevant_files"][:5]:
        content = _read_file_content(f["repo_path"], f["file"])
        if content:
            file_contents.append(f"### {f['file']}\n```{f['language'] or 'text'}\n{content}\n```")

    user_content = prompt
    if file_contents:
        user_content += "\n\n## Relevant File Contents (from Code Brain index)\n\n" + "\n\n".join(file_contents)

    messages = [{"role": "user", "content": user_content}]

    result = llm_chat(
        messages=messages,
        system_prompt=system_prompt,
        trace_id="code-agent",
        user_message=prompt,
        max_tokens=4096,
    )

    reply = result.get("reply", "")
    model = result.get("model", "unknown")

    import re
    diffs = re.findall(r"```diff\n(.*?)```", reply, re.DOTALL)
    files_changed = []
    for diff in diffs:
        for line in diff.splitlines():
            if line.startswith("---") or line.startswith("+++"):
                fname = line.split("\t")[0].replace("--- a/", "").replace("+++ b/", "").strip()
                if fname and fname != "/dev/null" and fname not in files_changed:
                    files_changed.append(fname)

    return {
        "response": reply,
        "model": model,
        "diffs": diffs,
        "files_changed": files_changed,
        "context_used": {
            "repos": len(context["repos"]),
            "insights": len(context["insights"]),
            "hotspots": len(context["hotspots"]),
            "relevant_files": len(context["relevant_files"]),
            "file_contents_included": len(file_contents),
        },
    }
