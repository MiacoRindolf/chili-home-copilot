"""Shared web research for Project Brain agents.

Uses DuckDuckGo via app.web_search and LLM summarization for producing
high-quality, agent-specific research entries.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import List

from sqlalchemy.orm import Session

from ...models.project_brain import AgentResearch
from ... import web_search as web_search_module
from ..llm_caller import call_llm

logger = logging.getLogger(__name__)


def _summarize_raw(topic: str, raw: str, trace_id: str) -> dict | None:
    """Use LLM to summarize raw search results into structured JSON."""
    prompt = (
        f"Summarize the following web search results about '{topic}'.\n\n"
        f"Raw results:\n{raw[:3000]}\n\n"
        "Return ONLY valid JSON:\n"
        "{\n"
        '  "summary": "3-6 sentence concise summary for a technical audience",\n'
        '  "sources": [{"title": "...", "url": "..."}],\n'
        '  "relevance_score": 0.0\n'
        "}\n"
    )
    reply = call_llm(
        messages=[
            {"role": "system", "content": "You are a precise summarization engine. Return only valid JSON."},
            {"role": "user", "content": prompt},
        ],
        max_tokens=500,
        trace_id=trace_id,
    )
    if not reply:
        return None
    start = reply.find("{")
    end = reply.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(reply[start : end + 1])
    except Exception:
        return None


def research_topics(
    db: Session,
    agent_name: str,
    user_id: int,
    topics: List[str],
    max_searches: int = 5,
    trace_id: str = "project_brain",
) -> List[AgentResearch]:
    """Search and store summaries for a list of topics."""
    results: List[AgentResearch] = []
    for topic in topics[:max_searches]:
        query = topic
        raw_results = web_search_module.search(query, max_results=5, trace_id=trace_id)
        if not raw_results:
            continue
        raw_text = json.dumps(raw_results, ensure_ascii=False)
        data = _summarize_raw(topic, raw_text, trace_id)
        if not data or not data.get("summary"):
            continue

        row = AgentResearch(
            agent_name=agent_name,
            user_id=user_id,
            topic=topic,
            query=query,
            summary=data["summary"],
            sources_json=json.dumps(data.get("sources", []), ensure_ascii=False),
            relevance_score=float(data.get("relevance_score", 0.0)),
            searched_at=datetime.utcnow(),
            stale=False,
        )
        db.add(row)
        results.append(row)
        logger.info("[%s] research saved topic=%r", agent_name, topic)

    if results:
        db.commit()
    return results
