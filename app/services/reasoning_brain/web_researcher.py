from __future__ import annotations

import json
from datetime import datetime
from typing import Iterable

from sqlalchemy.orm import Session

from ...config import settings
from ...logger import log_info
from ...models import ReasoningInterest, ReasoningResearch
from ... import web_search as web_search_module
from ... import openai_client


def _search_topic(topic: str, trace_id: str) -> str:
    """Run a DuckDuckGo search via existing web_search module and return text."""
    result = web_search_module.search(topic)
    if isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False)
    return str(result)


def refresh_research_for_top_interests(db: Session, user_id: int, trace_id: str = "reasoning") -> None:
    """Search and store summaries for the user's top interests."""
    if not settings.reasoning_enabled or not openai_client.is_configured():
        return

    max_searches = max(0, settings.reasoning_max_web_searches)
    if max_searches == 0:
        return

    interests: Iterable[ReasoningInterest] = (
        db.query(ReasoningInterest)
        .filter(
            ReasoningInterest.user_id == user_id,
            ReasoningInterest.active.is_(True),
        )
        .order_by(ReasoningInterest.weight.desc())
        .limit(max_searches)
        .all()
    )

    for interest in interests:
        topic = interest.topic
        raw = _search_topic(topic, trace_id)
        if not raw:
            continue

        prompt = (
            f"You are Chili's background researcher. Summarize the latest knowledge about '{topic}'.\n\n"
            "Use this raw search result payload (may be JSON or text):\n"
            f"{raw}\n\n"
            "Return ONLY valid JSON with keys:\n"
            "{\n"
            '  \"topic\": \"...\",\n'
            '  \"summary\": \"3-6 sentence concise explanation, assuming the user is technically savvy\",\n'
            '  \"sources\": [{\"title\": \"...\", \"url\": \"...\"}],\n'
            '  \"relevance_score\": 0.0\n'
            "}\n"
        )
        try:
            try:
                from ..context_brain.llm_gateway import gateway_chat
                result = gateway_chat(
                    messages=[{"role": "user", "content": prompt}],
                    purpose='reasoning_web_research',
                    system_prompt="You are a precise summarization engine. Return only valid JSON.",
                    trace_id=trace_id,
                )
            except Exception:
                result = openai_client.chat(
                    messages=[{"role": "user", "content": prompt}],
                    system_prompt="You are a precise summarization engine. Return only valid JSON.",
                    trace_id=trace_id,
                )
        except Exception as e:  # pragma: no cover - defensive
            log_info(trace_id, f"reasoning_web_research_error topic={topic!r} err={e}")
            continue

        if not result.get("reply"):
            continue

        text = result["reply"].strip()
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            continue
        try:
            data = json.loads(text[start : end + 1])
        except Exception as e:  # pragma: no cover
            log_info(trace_id, f"reasoning_web_research_parse_error topic={topic!r} err={e}")
            continue

        summary = data.get("summary") or ""
        if not summary:
            continue

        sources = data.get("sources") or []
        relevance = float(data.get("relevance_score") or 0.0)

        row = ReasoningResearch(
            user_id=user_id,
            topic=topic,
            summary=summary,
            sources=json.dumps(sources, ensure_ascii=False),
            relevance_score=relevance,
            searched_at=datetime.utcnow(),
            stale=False,
        )
        db.add(row)
        log_info(trace_id, f"reasoning_web_research_saved topic={topic!r}")

    db.commit()

