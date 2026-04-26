"""Retrievers — thin wrappers that adapt the existing CHILI context
sources to the Context Brain's ``ContextCandidate`` interface.

Every retriever exposes the same signature::

    def retrieve(message, *, db, user_id, project_id, trace_id) -> list[ContextCandidate]

so the orchestrator can fire them in parallel without per-source
custom calling conventions. Each one is wrapped in try/except so a
single broken source can never break the whole pipeline — it just
returns an empty list and logs.

We deliberately keep the bodies tiny: the *real* logic stays in the
existing modules (rag.py, personality.py, code_brain.learning,
reasoning_brain.learning, project_brain.registry, etc.). The Context
Brain owns scoring/ranking/budgeting; not retrieval mechanics.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from sqlalchemy.orm import Session

from .types import (
    SOURCE_CHAT_HISTORY,
    SOURCE_CODE_BRAIN,
    SOURCE_MEMORY,
    SOURCE_PERSONALITY,
    SOURCE_PLANNER,
    SOURCE_PROJECT_BRAIN,
    SOURCE_PROJECT_FILES,
    SOURCE_RAG,
    SOURCE_REASONING,
    ContextCandidate,
    estimate_tokens,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RAG retriever — wraps app/rag.py
# ---------------------------------------------------------------------------

def retrieve_rag(
    message: str,
    *,
    db: Session,
    user_id: Optional[int] = None,
    project_id: Optional[int] = None,
    trace_id: str = "",
) -> list[ContextCandidate]:
    try:
        from .. import rag as rag_module
    except Exception:
        try:
            import rag as rag_module  # type: ignore
        except Exception:
            return []
    try:
        hits = rag_module.search(message, n_results=3, trace_id=trace_id)
    except Exception as e:
        logger.debug("[context_brain.retrievers] rag failed: %s", e)
        return []

    out: list[ContextCandidate] = []
    for h in hits or []:
        if isinstance(h, dict):
            text = str(h.get("text") or h.get("content") or "")
            score = float(h.get("score") or 1.0 - float(h.get("distance") or 0.5))
            meta = {"source": h.get("source")}
        else:
            text = str(h)
            score = 0.5
            meta = {}
        if text.strip():
            out.append(ContextCandidate(
                source_id=SOURCE_RAG,
                content=text,
                raw_score=score,
                metadata=meta,
                tokens_estimated=estimate_tokens(text),
            ))
    return out


# ---------------------------------------------------------------------------
# Project files retriever — wraps app/services/project_file_service.py
# ---------------------------------------------------------------------------

def retrieve_project_files(
    message: str,
    *,
    db: Session,
    user_id: Optional[int] = None,
    project_id: Optional[int] = None,
    trace_id: str = "",
) -> list[ContextCandidate]:
    if not project_id:
        return []
    try:
        from .. import project_file_service as pfs
    except Exception:
        try:
            from ..services import project_file_service as pfs  # type: ignore
        except Exception:
            return []
    try:
        hits = pfs.search_project(project_id, message, n=3, trace_id=trace_id)  # type: ignore[attr-defined]
    except Exception as e:
        logger.debug("[context_brain.retrievers] project_files failed: %s", e)
        return []

    out: list[ContextCandidate] = []
    for h in hits or []:
        text = str(h.get("text") or h.get("content") or "") if isinstance(h, dict) else str(h)
        score = float(h.get("score") or 0.5) if isinstance(h, dict) else 0.5
        if text.strip():
            out.append(ContextCandidate(
                source_id=SOURCE_PROJECT_FILES,
                content=text,
                raw_score=score,
                metadata={"project_id": project_id, "source": h.get("source") if isinstance(h, dict) else None},
                tokens_estimated=estimate_tokens(text),
            ))
    return out


# ---------------------------------------------------------------------------
# Personality retriever — wraps app/personality.py
# ---------------------------------------------------------------------------

def retrieve_personality(
    message: str,
    *,
    db: Session,
    user_id: Optional[int] = None,
    project_id: Optional[int] = None,
    trace_id: str = "",
) -> list[ContextCandidate]:
    if user_id is None:
        return []
    try:
        from .. import personality as personality_module
        text = personality_module.get_profile_context(user_id, db)  # type: ignore[attr-defined]
    except Exception as e:
        logger.debug("[context_brain.retrievers] personality failed: %s", e)
        return []
    text = (text or "").strip()
    if not text:
        return []
    return [ContextCandidate(
        source_id=SOURCE_PERSONALITY,
        content=text,
        raw_score=1.0,  # always relevant; scorer applies recency separately
        tokens_estimated=estimate_tokens(text),
    )]


# ---------------------------------------------------------------------------
# Memory retriever — wraps app/memory.py
# ---------------------------------------------------------------------------

def retrieve_memory(
    message: str,
    *,
    db: Session,
    user_id: Optional[int] = None,
    project_id: Optional[int] = None,
    trace_id: str = "",
) -> list[ContextCandidate]:
    if user_id is None:
        return []
    try:
        from .. import memory as memory_module
        text = memory_module.get_memory_context(user_id, db)  # type: ignore[attr-defined]
    except Exception as e:
        logger.debug("[context_brain.retrievers] memory failed: %s", e)
        return []
    text = (text or "").strip()
    if not text:
        return []
    return [ContextCandidate(
        source_id=SOURCE_MEMORY,
        content=text,
        raw_score=0.9,
        tokens_estimated=estimate_tokens(text),
    )]


# ---------------------------------------------------------------------------
# Code brain retriever — wraps app/services/code_brain/learning.py
# ---------------------------------------------------------------------------

def retrieve_code_brain(
    message: str,
    *,
    db: Session,
    user_id: Optional[int] = None,
    project_id: Optional[int] = None,
    trace_id: str = "",
) -> list[ContextCandidate]:
    if user_id is None:
        return []
    try:
        from ..code_brain.learning import get_project_chat_context  # type: ignore[attr-defined]
        text = get_project_chat_context(db, user_id)
    except Exception as e:
        logger.debug("[context_brain.retrievers] code_brain failed: %s", e)
        return []
    text = (text or "").strip()
    if not text:
        return []
    return [ContextCandidate(
        source_id=SOURCE_CODE_BRAIN,
        content=text,
        raw_score=0.95,  # the code brain summary is dense + curated
        tokens_estimated=estimate_tokens(text),
    )]


# ---------------------------------------------------------------------------
# Reasoning brain retriever — wraps app/services/reasoning_brain/learning.py
# ---------------------------------------------------------------------------

def retrieve_reasoning(
    message: str,
    *,
    db: Session,
    user_id: Optional[int] = None,
    project_id: Optional[int] = None,
    trace_id: str = "",
) -> list[ContextCandidate]:
    if user_id is None:
        return []
    try:
        from ..reasoning_brain.learning import get_reasoning_chat_context  # type: ignore[attr-defined]
        text = get_reasoning_chat_context(db, user_id)
    except Exception as e:
        logger.debug("[context_brain.retrievers] reasoning failed: %s", e)
        return []
    text = (text or "").strip()
    if not text:
        return []
    return [ContextCandidate(
        source_id=SOURCE_REASONING,
        content=text,
        raw_score=0.85,
        tokens_estimated=estimate_tokens(text),
    )]


# ---------------------------------------------------------------------------
# Project brain retriever — wraps app/services/chat_context.py helper
# ---------------------------------------------------------------------------

def retrieve_project_brain(
    message: str,
    *,
    db: Session,
    user_id: Optional[int] = None,
    project_id: Optional[int] = None,
    trace_id: str = "",
) -> list[ContextCandidate]:
    if user_id is None:
        return []
    try:
        from .. import chat_context  # type: ignore[attr-defined]
        text = chat_context.get_project_brain_context_for_chat(db, user_id)  # type: ignore[attr-defined]
    except Exception as e:
        logger.debug("[context_brain.retrievers] project_brain failed: %s", e)
        return []
    text = (text or "").strip()
    if not text:
        return []
    return [ContextCandidate(
        source_id=SOURCE_PROJECT_BRAIN,
        content=text,
        raw_score=0.85,
        tokens_estimated=estimate_tokens(text),
    )]


# ---------------------------------------------------------------------------
# Planner retriever — wraps planner_service.get_user_project_summary
# ---------------------------------------------------------------------------

def retrieve_planner(
    message: str,
    *,
    db: Session,
    user_id: Optional[int] = None,
    project_id: Optional[int] = None,
    trace_id: str = "",
) -> list[ContextCandidate]:
    if user_id is None:
        return []
    try:
        from ..services import planner_service as ps  # type: ignore[no-redef]
    except Exception:
        try:
            from .. import planner_service as ps  # type: ignore[attr-defined,no-redef]
        except Exception:
            return []
    try:
        text = ps.get_user_project_summary(db, user_id)  # type: ignore[attr-defined]
    except Exception as e:
        logger.debug("[context_brain.retrievers] planner failed: %s", e)
        return []
    text = (text or "").strip()
    if not text:
        return []
    return [ContextCandidate(
        source_id=SOURCE_PLANNER,
        content=text,
        raw_score=0.8,
        tokens_estimated=estimate_tokens(text),
    )]


# ---------------------------------------------------------------------------
# Chat history retriever — last N messages for this convo
# ---------------------------------------------------------------------------

def retrieve_chat_history(
    message: str,
    *,
    db: Session,
    user_id: Optional[int] = None,
    project_id: Optional[int] = None,
    trace_id: str = "",
    conversation_id: Optional[int] = None,
    limit: int = 8,
) -> list[ContextCandidate]:
    if user_id is None:
        return []
    try:
        from sqlalchemy import text as _t
        if conversation_id is not None:
            rows = db.execute(
                _t(
                    "SELECT role, content, created_at FROM chat_messages "
                    "WHERE conversation_id = :cid "
                    "ORDER BY id DESC LIMIT :lim"
                ),
                {"cid": int(conversation_id), "lim": int(limit)},
            ).fetchall()
        else:
            rows = db.execute(
                _t(
                    "SELECT role, content, created_at FROM chat_messages "
                    "WHERE user_id = :uid "
                    "ORDER BY id DESC LIMIT :lim"
                ),
                {"uid": int(user_id), "lim": int(limit)},
            ).fetchall()
    except Exception as e:
        logger.debug("[context_brain.retrievers] chat_history failed: %s", e)
        return []

    if not rows:
        return []

    # Stitch in chronological order
    rows = list(reversed(rows))
    lines = []
    for r in rows:
        role = (r[0] or "").strip() or "user"
        content = (r[1] or "").strip()
        if not content:
            continue
        lines.append(f"[{role}] {content}")
    text = "\n".join(lines)
    if not text.strip():
        return []
    return [ContextCandidate(
        source_id=SOURCE_CHAT_HISTORY,
        content=text,
        raw_score=0.95,  # very high — recent conversation is highly relevant
        metadata={"message_count": len(rows)},
        tokens_estimated=estimate_tokens(text),
    )]


# ---------------------------------------------------------------------------
# Registry — orchestrator looks up callables by source_id
# ---------------------------------------------------------------------------

RetrieverFn = Callable[..., list[ContextCandidate]]

REGISTRY: dict[str, RetrieverFn] = {
    SOURCE_RAG: retrieve_rag,
    SOURCE_PROJECT_FILES: retrieve_project_files,
    SOURCE_PERSONALITY: retrieve_personality,
    SOURCE_MEMORY: retrieve_memory,
    SOURCE_CODE_BRAIN: retrieve_code_brain,
    SOURCE_REASONING: retrieve_reasoning,
    SOURCE_PROJECT_BRAIN: retrieve_project_brain,
    SOURCE_PLANNER: retrieve_planner,
    SOURCE_CHAT_HISTORY: retrieve_chat_history,
}
