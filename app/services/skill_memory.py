"""Semantic index of teacher-learned skills — closes the P4 loop.

Teacher-escalation (app/teacher_escalation.py) writes reusable skills to a JSONL
audit log. This module additionally indexes them into a dedicated ChromaDB
collection so the weak/local model can RETRIEVE relevant learned procedures by
similarity — turning the audit log into usable memory. Reuses app/rag.py's Chroma
client + Ollama embedding function (same store, separate collection).

Best-effort and fully guarded: if Chroma/Ollama is unavailable, index/retrieve
degrade to no-ops (return False / []) and never raise into a caller — so this is
safe to call from the dormant teacher-escalation path.

Public:
    index_skill(skill) -> bool
    retrieve_skills(query, k=3) -> list[dict]   # ready for prompt-wiring (deferred)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_COLLECTION = "teacher_skills"


def _collection(read_only: bool = False):
    """Get (or create) the teacher-skills Chroma collection, or None if the
    vector store / embedding backend is unavailable."""
    try:
        import chromadb
        from .. import rag
        rag.CHROMA_DIR.mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(path=str(rag.CHROMA_DIR))
        ef = rag._get_embedding_function()
        if read_only:
            try:
                return client.get_collection(name=_COLLECTION, embedding_function=ef)
            except Exception:
                return None
        return client.get_or_create_collection(name=_COLLECTION, embedding_function=ef)
    except Exception as e:
        logger.warning("[skill_memory] collection unavailable: %s", e)
        return None


def _skill_doc(skill: Dict[str, Any]) -> str:
    """Flatten a skill dict into an embeddable document string."""
    parts: List[str] = [
        str(skill.get("name") or ""),
        str(skill.get("description") or ""),
        str(skill.get("when_to_use") or ""),
    ]
    proc = skill.get("procedure")
    if isinstance(proc, list):
        parts.extend(str(s) for s in proc)
    return "\n".join(p for p in parts if p).strip()


def index_skill(skill: Dict[str, Any]) -> bool:
    """Upsert a teacher-written skill into the semantic index. Returns success.

    Keyed by skill name (re-learning the same skill updates its vector). Never
    raises.
    """
    if not isinstance(skill, dict):
        return False
    name = (skill.get("name") or "").strip()
    doc = _skill_doc(skill)
    if not name or not doc:
        return False
    col = _collection(read_only=False)
    if col is None:
        return False
    try:
        col.upsert(
            ids=[name],
            documents=[doc],
            metadatas=[{
                "name": name,
                "description": str(skill.get("description") or ""),
                "source": str(skill.get("source") or "teacher-escalation"),
            }],
        )
        logger.info("[skill_memory] indexed skill %r", name)
        return True
    except Exception as e:
        logger.warning("[skill_memory] index failed for %r: %s", name, e)
        return False


def retrieve_skills(query: str, k: int = 3) -> List[Dict[str, Any]]:
    """Return up to k learned skills most relevant to `query` (best-effort).

    Each result: {"name", "description", "document"}. Empty list if nothing is
    indexed or the store is unavailable. Never raises. Ready for prompt-wiring
    (injecting retrieved procedures into the weak model's context) — that hook is
    deferred since it touches the LLM prompt path.
    """
    query = (query or "").strip()
    if not query:
        return []
    col = _collection(read_only=True)
    if col is None:
        return []
    try:
        res = col.query(query_texts=[query], n_results=max(1, int(k)))
        metas = (res.get("metadatas") or [[]])[0] or []
        docs = (res.get("documents") or [[]])[0] or []
        out: List[Dict[str, Any]] = []
        for meta, doc in zip(metas, docs):
            meta = meta or {}
            out.append({
                "name": meta.get("name", ""),
                "description": meta.get("description", ""),
                "document": doc,
            })
        return out
    except Exception as e:
        logger.warning("[skill_memory] retrieve failed: %s", e)
        return []
