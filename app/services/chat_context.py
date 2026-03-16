"""Chat context gathering: RAG, personality, memory, project, code brain, reasoning.

Extracted from chat_service.py to reduce complexity. These functions handle
the parallel context-gathering pipeline that feeds the LLM system prompt.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from sqlalchemy.orm import Session

from ..db import SessionLocal


def _thread_get_personality_memory(user_id: int) -> str | None:
    """Thread-safe: use own session for personality + memory context."""
    from . import personality as personality_module
    from . import memory as memory_module
    s = SessionLocal()
    try:
        personality = personality_module.get_profile_context(user_id, s)
        memory = memory_module.get_memory_context(user_id, s)
        if memory and personality:
            personality = personality + "\n\n" + memory
        elif memory:
            personality = memory
        return personality
    finally:
        s.close()


def _thread_get_project_summary(user_id: int) -> str | None:
    """Thread-safe: use own session for planner project summary."""
    from ..modules import is_module_enabled
    if not is_module_enabled("planner"):
        return None
    from . import planner_service
    s = SessionLocal()
    try:
        return planner_service.get_user_project_summary(s, user_id)
    finally:
        s.close()


def gather_context_parallel(
    message: str,
    identity: dict,
    trace_id: str,
    project_id: int | None = None,
) -> tuple[str | None, list, str | None, str | None]:
    """Run RAG, project RAG, personality+memory, and project summary in parallel.
    Returns (rag_context, rag_hits, personality_context, project_context)."""
    from . import rag as rag_module
    from . import project_file_service as pfs_module
    from ..modules import is_module_enabled
    from ..logging_utils import log_info

    is_guest = identity["is_guest"]
    user_id = identity.get("user_id")
    rag_context = None
    rag_hits = []
    personality_context = None
    project_context = None

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {}
        futures["rag"] = executor.submit(rag_module.search, message, 3, trace_id)
        if project_id:
            futures["proj_rag"] = executor.submit(
                pfs_module.search_project, project_id, message, 3, trace_id
            )
        if user_id and not is_guest:
            futures["personality"] = executor.submit(_thread_get_personality_memory, user_id)
            if is_module_enabled("planner"):
                futures["project"] = executor.submit(_thread_get_project_summary, user_id)

        rag_hits = futures["rag"].result()
        if rag_hits and rag_hits[0]["distance"] < 1.0:
            rag_context = "\n---\n".join(f"[{h['source']}]: {h['text']}" for h in rag_hits)
            log_info(trace_id, f"rag_context_injected sources={[h['source'] for h in rag_hits]}")

        if project_id and "proj_rag" in futures:
            proj_hits = futures["proj_rag"].result()
            if proj_hits:
                proj_context = "\n---\n".join(f"[project:{h['source']}]: {h['text']}" for h in proj_hits)
                rag_context = f"{proj_context}\n---\n{rag_context}" if rag_context else proj_context
                rag_hits = proj_hits + (rag_hits or [])
                log_info(trace_id, f"project_rag_injected project={project_id}")

        if user_id and not is_guest and "personality" in futures:
            personality_context = futures["personality"].result()
            if personality_context:
                log_info(trace_id, f"personality_injected user_id={user_id}")
            if "project" in futures:
                project_context = futures["project"].result()
                if project_context:
                    log_info(trace_id, f"project_context_injected user_id={user_id}")
                    personality_context = (personality_context or "") + "\n\n" + project_context

    return (rag_context, rag_hits, personality_context, project_context)


def gather_context_only(
    db: Session,
    message: str,
    identity: dict,
    trace_id: str,
    project_id: int | None = None,
):
    """Gather RAG, personality, memory, and project context without calling Ollama."""
    rag_context, rag_hits, personality_context, _ = gather_context_parallel(
        message, identity, trace_id, project_id
    )
    return {
        "planned": {"type": "unknown", "data": {}, "reply": ""},
        "rag_context": rag_context,
        "rag_hits": rag_hits if rag_context else [],
        "personality_context": personality_context,
    }


def build_openai_prompt(
    user_name: str,
    personality_context: str | None,
    rag_context: str | None,
    base_system_prompt: str = "",
    planner_context: bool = False,
    code_context: str | None = None,
    reasoning_context: str | None = None,
) -> str:
    """Build the OpenAI system prompt with personality, RAG, planner, code, and reasoning context."""
    openai_system = base_system_prompt
    openai_system += f"\n\nYou are talking to: {user_name}."
    if personality_context:
        openai_system += f"\n\n{personality_context}"
    if rag_context:
        openai_system += f"\n\nHousehold document context (use ONLY if the user asks about these topics -- do NOT volunteer this info unprompted):\n{rag_context}"
    if planner_context:
        from ..prompts import load_prompt
        openai_system += "\n\n" + load_prompt("planner_page_context")
    if code_context:
        openai_system += (
            "\n\nProject codebase context (use this when the user asks coding, refactoring, or debugging questions; "
            "otherwise ignore it):\n"
            f"{code_context}"
        )
    if reasoning_context:
        openai_system += (
            "\n\nUser reasoning and preference snapshot (use this to match their style, anticipate needs, and frame answers "
            "in terms of their active goals; do NOT overfit or make creepy predictions):\n"
            f"{reasoning_context}"
        )
    return openai_system
