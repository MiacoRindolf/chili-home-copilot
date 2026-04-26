"""Compose the final structured prompt + write the assembly_log row.

The composer owns:
  1. Putting selected candidates into a stable, structured XML-tagged
     order that gpt-5.x parses well (and that survives token-counting).
  2. Writing one ``context_assembly_log`` row + N ``context_candidate_log``
     rows so the learning loop and the operator UI have a complete
     record of what went into this turn.

The prompt structure:

    <context>
      <intent>code (confidence=0.91)</intent>
      <chat_history>...</chat_history>
      <code_brain>...</code_brain>
      <project_brain>...</project_brain>
      <rag>...</rag>
      <memory>...</memory>
      <personality>...</personality>
      <reasoning>...</reasoning>
      <planner>...</planner>
      <project_files>...</project_files>
    </context>

    User's message:
    <user_message>...</user_message>

XML-tagged because Anthropic and OpenAI's reasoning models both
respect well-formed tags as section boundaries far better than
markdown headers, and it's robust against accidental tag-like
content inside RAG hits (we escape ``<`` in candidate content).
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Optional

from sqlalchemy import text
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
    AssembledContext,
    ContextCandidate,
    IntentClassification,
)

logger = logging.getLogger(__name__)


# Section ordering inside the <context> block. More-grounded sources
# come first so the LLM weights them earlier in its attention.
_SECTION_ORDER = [
    SOURCE_CHAT_HISTORY,
    SOURCE_CODE_BRAIN,
    SOURCE_PROJECT_BRAIN,
    SOURCE_REASONING,
    SOURCE_PLANNER,
    SOURCE_RAG,
    SOURCE_PROJECT_FILES,
    SOURCE_MEMORY,
    SOURCE_PERSONALITY,
]


def _escape_xml_lite(s: str) -> str:
    # Just enough to keep stray '<' or '>' from breaking section parsing.
    # We do NOT do full XML escaping because the LLM is fine with raw
    # ampersands and quotes; only angle brackets confuse it.
    return s.replace("<", "&lt;").replace(">", "&gt;")


def compose_prompt(
    user_message: str,
    intent: IntentClassification,
    candidates: list[ContextCandidate],
) -> str:
    """Build the final prompt string that goes to ``openai_client.chat()``."""
    by_source: dict[str, list[ContextCandidate]] = {}
    for c in candidates:
        if not c.selected:
            continue
        by_source.setdefault(c.source_id, []).append(c)

    lines: list[str] = []
    lines.append("<context>")
    lines.append(f"  <intent>{_escape_xml_lite(intent.intent)} (confidence={intent.confidence:.2f})</intent>")

    for src in _SECTION_ORDER:
        items = by_source.get(src, [])
        if not items:
            continue
        lines.append(f"  <{src}>")
        for c in items:
            content = _escape_xml_lite(c.content.strip())
            lines.append(content)
            lines.append("  ---")
        # Trim trailing separator
        if lines[-1] == "  ---":
            lines.pop()
        lines.append(f"  </{src}>")

    lines.append("</context>")
    lines.append("")
    lines.append("User's message:")
    lines.append("<user_message>")
    lines.append(_escape_xml_lite(user_message))
    lines.append("</user_message>")
    return "\n".join(lines)


def _query_hash(message: str) -> str:
    return hashlib.sha256((message or "").encode("utf-8")).hexdigest()[:32]


def write_assembly_log(
    db: Session,
    *,
    user_message: str,
    intent: IntentClassification,
    candidates: list[ContextCandidate],
    total_tokens: int,
    budget_token_cap: int,
    strategy_version: int,
    elapsed_ms: int,
    distilled: bool = False,
    distillation_tokens_saved: int = 0,
    chat_message_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> Optional[int]:
    """Write one assembly_log row + per-candidate rows. Returns assembly_id.

    Wrapped in try/except so logging failures never break the chat path.
    """
    try:
        sources_used = {}
        for c in candidates:
            if c.selected:
                sources_used[c.source_id] = sources_used.get(c.source_id, 0) + 1

        budget_used_pct = (
            round(100.0 * total_tokens / max(1, budget_token_cap), 2)
            if budget_token_cap > 0 else 0
        )

        row = db.execute(
            text(
                "INSERT INTO context_assembly_log "
                "(chat_message_id, user_id, intent, intent_confidence, query_hash, "
                " sources_used, total_tokens_input, budget_token_cap, budget_used_pct, "
                " strategy_version, distilled, distillation_tokens_saved, elapsed_ms) "
                "VALUES (:cmid, :uid, :intent, :conf, :qh, CAST(:sources AS jsonb), "
                "        :ttok, :bcap, :bpct, :sv, :dist, :dsave, :elapsed) "
                "RETURNING id"
            ),
            {
                "cmid": chat_message_id,
                "uid": user_id,
                "intent": intent.intent,
                "conf": intent.confidence,
                "qh": _query_hash(user_message),
                "sources": json.dumps(sources_used),
                "ttok": int(total_tokens),
                "bcap": int(budget_token_cap),
                "bpct": budget_used_pct,
                "sv": int(strategy_version),
                "dist": bool(distilled),
                "dsave": int(distillation_tokens_saved),
                "elapsed": int(elapsed_ms),
            },
        ).fetchone()
        if not row:
            db.commit()
            return None
        assembly_id = int(row[0])

        # Bulk insert per-candidate rows
        for c in candidates:
            db.execute(
                text(
                    "INSERT INTO context_candidate_log "
                    "(assembly_id, source_id, raw_score, relevance_score, "
                    " final_weight, selected, tokens_estimated, content_hash, "
                    " distilled, preview) "
                    "VALUES (:aid, :src, :raw, :rel, :fw, :sel, :tk, :ch, :dist, :pv)"
                ),
                {
                    "aid": assembly_id,
                    "src": c.source_id,
                    "raw": float(c.raw_score),
                    "rel": float(c.relevance_score),
                    "fw": float(c.final_weight),
                    "sel": bool(c.selected),
                    "tk": int(c.tokens_estimated),
                    "ch": c.content_hash,
                    "dist": bool(c.distilled),
                    "pv": c.preview,
                },
            )
        db.commit()
        return assembly_id
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("[context_brain.composer] assembly_log write failed: %s", e)
        return None
