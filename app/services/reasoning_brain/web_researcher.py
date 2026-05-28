from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable

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


def _raw_search_items(raw: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(raw or "[]")
    except Exception:
        return []
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    if isinstance(parsed, dict):
        for key in ("results", "items", "organic_results", "news"):
            value = parsed.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _source_from_result(item: dict[str, Any]) -> dict[str, str] | None:
    title = str(item.get("title") or item.get("name") or "").strip()
    url = str(item.get("href") or item.get("url") or item.get("link") or "").strip()
    if not title and not url:
        return None
    return {"title": title or url, "url": url}


def _snippet_from_result(item: dict[str, Any]) -> str:
    return str(
        item.get("body")
        or item.get("snippet")
        or item.get("description")
        or item.get("summary")
        or ""
    ).strip()


def _mechanical_research_summary(topic: str, raw: str, *, max_items: int = 4) -> dict | None:
    """Summarize structured search results without an LLM."""
    items = _raw_search_items(raw)
    if not items:
        return None

    snippets: list[str] = []
    sources: list[dict[str, str]] = []
    seen_sources: set[str] = set()
    for item in items:
        source = _source_from_result(item)
        if source:
            key = (source.get("url") or source.get("title") or "").casefold()
            if key and key not in seen_sources:
                sources.append(source)
                seen_sources.add(key)
        snippet = _snippet_from_result(item)
        if snippet:
            snippets.append(snippet.strip(" ."))
        if len(snippets) >= max_items and len(sources) >= max_items:
            break

    if not snippets:
        titles = [str(item.get("title") or item.get("name") or "").strip() for item in items]
        snippets = [title for title in titles if title][:max_items]
    if not snippets:
        return None

    summary_bits = snippets[:max_items]
    summary = f"Search results for {topic} highlight: " + " ".join(
        f"{bit}." for bit in summary_bits if bit
    )
    return {
        "topic": topic,
        "summary": summary[:1200],
        "sources": sources[:max_items],
        "relevance_score": min(1.0, 0.45 + 0.1 * min(len(snippets), max_items)),
    }


def _parse_research_reply(reply: str, trace_id: str, topic: str) -> dict | None:
    text = (reply or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except Exception as e:  # pragma: no cover
        log_info(trace_id, f"reasoning_web_research_parse_error topic={topic!r} err={e}")
        return None
    return data if isinstance(data, dict) else None


def _has_fresh_research(db: Session, user_id: int, topic: str) -> bool:
    try:
        row = (
            db.query(ReasoningResearch)
            .filter(
                ReasoningResearch.user_id == user_id,
                ReasoningResearch.topic == topic,
                ReasoningResearch.stale.is_(False),
            )
            .order_by(ReasoningResearch.searched_at.desc())
            .first()
        )
    except Exception:
        return False
    return row is not None


def _store_research_row(db: Session, *, user_id: int, topic: str, data: dict, trace_id: str, source: str) -> bool:
    summary = data.get("summary") or ""
    if not summary:
        return False

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
    log_info(trace_id, f"reasoning_web_research_saved source={source} topic={topic!r}")
    return True


def refresh_research_for_top_interests(db: Session, user_id: int, trace_id: str = "reasoning") -> None:
    """Search and store summaries for the user's top interests."""
    if not settings.reasoning_enabled:
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
        if _has_fresh_research(db, user_id, topic):
            continue

        raw = _search_topic(topic, trace_id)
        if not raw:
            continue

        mechanical = _mechanical_research_summary(topic, raw)
        if mechanical and _store_research_row(
            db,
            user_id=user_id,
            topic=topic,
            data=mechanical,
            trace_id=trace_id,
            source="mechanical",
        ):
            continue

        if not openai_client.is_configured():
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
            except Exception as e:
                log_info(trace_id, f"reasoning_web_research_gateway_error topic={topic!r} err={e}")
                continue
        except Exception as e:  # pragma: no cover - defensive
            log_info(trace_id, f"reasoning_web_research_error topic={topic!r} err={e}")
            continue

        if not result.get("reply"):
            continue

        data = _parse_research_reply(result["reply"], trace_id, topic)
        if not data:
            continue

        _store_research_row(
            db,
            user_id=user_id,
            topic=topic,
            data=data,
            trace_id=trace_id,
            source="gateway",
        )

    db.commit()

